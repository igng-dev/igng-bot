"""Internal client for the isolated terminal-broker service."""

import asyncio
import re
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout


class BrokerClientError(Exception):
    """Expected broker failure; messages are safe to surface to the caller."""


HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
DEFAULT_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def _check_handle(value, pattern, label):
    text = str(value or "")
    if not pattern.fullmatch(text):
        raise BrokerClientError(f"invalid {label}")
    return text


class BrokerClient:
    def __init__(self, base_url, secret, *, timeout=20, max_download_bytes=DEFAULT_MAX_DOWNLOAD_BYTES):
        base_url = str(base_url or "").rstrip("/")
        secret = str(secret or "")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("invalid broker url")
        if len(secret) < 32:
            raise ValueError("broker secret must contain at least 32 characters")
        self.base_url = base_url
        self.secret = secret
        self.timeout = float(timeout)
        self.max_download_bytes = int(max_download_bytes)

    def _headers(self, owner=None, extra=None):
        headers = {"Authorization": f"Bearer {self.secret}"}
        if owner is not None:
            headers["X-Session-Owner"] = _check_handle(owner, HANDLE_RE, "owner")
        if extra:
            headers.update(extra)
        return headers

    async def register(self, owner, *, ttl=None):
        owner = _check_handle(owner, HANDLE_RE, "owner")
        body = {"owner": owner}
        if ttl is not None:
            body["ttlSec"] = int(ttl)
        async with ClientSession(timeout=ClientTimeout(total=self.timeout)) as http:
            async with http.post(f"{self.base_url}/v1/sessions", json=body, headers=self._headers()) as response:
                if response.status != 200:
                    raise BrokerClientError("broker session registration rejected")
                payload = await response.json()
        session_id = _check_handle(payload.get("sessionId"), HANDLE_RE, "session")
        bind_token = _check_handle(payload.get("bindToken"), HANDLE_RE, "bind token")
        return {"sessionId": session_id, "bindToken": bind_token, "expiresInSec": int(payload.get("expiresInSec") or 0)}

    async def bind(self, session_id, bind_token, owner):
        session_id = _check_handle(session_id, HANDLE_RE, "session")
        owner = _check_handle(owner, HANDLE_RE, "owner")
        bind_token = str(bind_token or "")
        if not bind_token or len(bind_token.encode()) > 256:
            raise BrokerClientError("invalid bind token")
        async with ClientSession(timeout=ClientTimeout(total=self.timeout)) as http:
            async with http.post(
                f"{self.base_url}/v1/sessions/{session_id}/bind",
                json={"bindToken": bind_token, "owner": owner},
                headers=self._headers(),
            ) as response:
                if response.status != 200:
                    raise BrokerClientError("broker session binding rejected")
                return await response.json()

    async def close_session(self, session_id, owner):
        session_id = _check_handle(session_id, HANDLE_RE, "session")
        async with ClientSession(timeout=ClientTimeout(total=self.timeout)) as http:
            async with http.delete(
                f"{self.base_url}/v1/sessions/{session_id}",
                headers=self._headers(owner),
            ) as response:
                return response.status == 200

    async def add_input(self, session_id, owner, data, *, name, media_type):
        session_id = _check_handle(session_id, HANDLE_RE, "session")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise BrokerClientError("input must be non-empty bytes")
        timeout = ClientTimeout(total=max(self.timeout, 120))
        async with ClientSession(timeout=timeout) as http:
            async with http.post(
                f"{self.base_url}/v1/sessions/{session_id}/inputs",
                data=bytes(data),
                headers=self._headers(owner, {"X-Input-Name": str(name)[:120], "X-Input-Media-Type": str(media_type)[:120]}),
            ) as response:
                if response.status != 200:
                    raise BrokerClientError("broker rejected input")
                payload = await response.json()
        return payload

    async def run_task(self, session_id, owner, operation, payload, *, timeout=600):
        session_id = _check_handle(session_id, HANDLE_RE, "session")
        owner = _check_handle(owner, HANDLE_RE, "owner")
        timeout = float(timeout)
        task_id = None
        try:
            async with ClientSession(timeout=ClientTimeout(total=min(30, self.timeout))) as http:
                async with http.post(
                    f"{self.base_url}/v1/sessions/{session_id}/tasks",
                    json={"operation": operation, **payload},
                    headers=self._headers(owner, {"content-type": "application/json"}),
                ) as response:
                    if response.status != 200:
                        raise BrokerClientError("broker rejected task")
                    created = await response.json()
                task_id = _check_handle(created.get("taskId"), UUID_RE, "task")
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise BrokerClientError("broker task timed out")
                    await asyncio.sleep(0.2)
                    async with http.get(
                        f"{self.base_url}/v1/sessions/{session_id}/tasks/{task_id}",
                        headers=self._headers(owner),
                    ) as response:
                        if response.status != 200:
                            raise BrokerClientError("broker task lookup failed")
                        status = await response.json()
                    if status.get("status") == "completed":
                        return status.get("result") or {}
                    if status.get("status") in {"failed", "cancelled"}:
                        raise BrokerClientError(str(status.get("error") or "broker task failed"))
        except asyncio.CancelledError:
            if task_id:
                await self._cancel_quietly(session_id, owner, task_id)
            raise
        except BrokerClientError:
            if task_id:
                await self._cancel_quietly(session_id, owner, task_id)
            raise

    async def _cancel_quietly(self, session_id, owner, task_id):
        try:
            await self.cancel_task(session_id, owner, task_id)
        except (BrokerClientError, asyncio.TimeoutError, OSError):
            pass

    async def cancel_task(self, session_id, owner, task_id):
        session_id = _check_handle(session_id, HANDLE_RE, "session")
        task_id = _check_handle(task_id, UUID_RE, "task")
        async with ClientSession(timeout=ClientTimeout(total=self.timeout)) as http:
            async with http.delete(
                f"{self.base_url}/v1/sessions/{session_id}/tasks/{task_id}",
                headers=self._headers(owner),
            ) as response:
                if response.status != 200:
                    raise BrokerClientError("broker task cancellation failed")
                return await response.json()

    async def download_artifact(self, session_id, owner, artifact_id):
        session_id = _check_handle(session_id, HANDLE_RE, "session")
        artifact_id = _check_handle(artifact_id, UUID_RE, "artifact")
        async with ClientSession(timeout=ClientTimeout(total=max(self.timeout, 120))) as http:
            async with http.get(
                f"{self.base_url}/v1/sessions/{session_id}/artifacts/{artifact_id}",
                headers=self._headers(owner),
            ) as response:
                if response.status != 200:
                    raise BrokerClientError("broker artifact lookup failed")
                length = response.content_length
                if length is not None and length > self.max_download_bytes:
                    raise BrokerClientError("artifact exceeds delivery limit")
                data = await response.read()
                if not data or len(data) > self.max_download_bytes:
                    raise BrokerClientError("artifact exceeds delivery limit")
                return {
                    "data": data,
                    "name": response.headers.get("X-Artifact-Name", "artifact.bin"),
                    "mediaType": response.headers.get("X-Artifact-Media-Type", "application/octet-stream"),
                    "artifactId": response.headers.get("X-Artifact-Id", artifact_id),
                    "size": len(data),
                }
