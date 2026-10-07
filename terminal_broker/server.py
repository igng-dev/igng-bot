"""Controlled video operations with opaque session-scoped handles.

The broker receives attachment bytes from the Bot over an internal network.
It never mounts the Bot's NAS storage and never accepts shell commands or
filesystem paths from callers.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web


logger = logging.getLogger("yunying.terminal_broker")

HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")
MEDIA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.+-]{0,60}/[A-Za-z0-9.+-]{1,60}$")

DEFAULT_INPUT_BYTES = 256 * 1024 * 1024
DEFAULT_ARTIFACT_BYTES = 256 * 1024 * 1024
DEFAULT_SESSION_TTL = 30 * 60
MAX_SESSION_TTL = 2 * 60 * 60
MAX_TASKS_PER_SESSION = 16
MAX_INPUTS_PER_SESSION = 8
MAX_ARTIFACTS_PER_SESSION = 24
MAX_BIND_TOKEN_BYTES = 256


class BrokerError(Exception):
    """Expected, safe-to-return broker error."""


def _safe_name(value, fallback="artifact"):
    name = NAME_RE.sub("_", str(value or "").strip()).strip("._")
    return name[:120] or fallback


def _media_type(value, fallback="application/octet-stream"):
    text = str(value or "").strip()
    return text if MEDIA_RE.fullmatch(text) else fallback


def _handle(value, *, kind="handle"):
    text = str(value or "")
    if not HANDLE_RE.fullmatch(text):
        raise BrokerError(f"invalid {kind}")
    return text


def _uuid_handle(value, *, kind="id"):
    text = str(value or "")
    if not UUID_RE.fullmatch(text):
        raise BrokerError(f"invalid {kind}")
    return text


def _bounded_number(value, default, minimum, maximum, *, integer=False):
    if value is None:
        return default
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise BrokerError("numeric parameter is invalid")
    if number < minimum or number > maximum:
        raise BrokerError("numeric parameter is out of range")
    return number


def _safe_probe(value, depth=0):
    """Remove path-like metadata before returning ffprobe data to DSH."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        blocked = {"filename", "url", "uri", "path", "file", "extradata"}
        return {
            str(key): _safe_probe(child, depth + 1)
            for key, child in value.items()
            if str(key).lower() not in blocked
        }
    if isinstance(value, list):
        return [_safe_probe(child, depth + 1) for child in value[:100]]
    if isinstance(value, str):
        return value[:4000]
    return value


@dataclass
class Artifact:
    artifact_id: str
    path: Path
    name: str
    media_type: str
    size: int
    task_id: str
    created_at: float = field(default_factory=time.monotonic)


@dataclass
class Task:
    task_id: str
    operation: str
    created_at: float = field(default_factory=time.monotonic)
    status: str = "queued"
    result: dict | None = None
    error: str | None = None
    runner: asyncio.Task | None = None


@dataclass
class Session:
    session_id: str
    directory: Path
    owner: str
    bound: bool = False
    bind_digest: str | None = None
    expires_at: float = 0.0
    created_at: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)
    inputs: dict[str, Path] = field(default_factory=dict)
    input_names: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)


class BrokerState:
    """Session and process lifecycle manager used by the HTTP surface."""

    def __init__(
        self,
        root=None,
        *,
        input_limit=DEFAULT_INPUT_BYTES,
        artifact_limit=DEFAULT_ARTIFACT_BYTES,
        session_ttl=DEFAULT_SESSION_TTL,
    ):
        self.root = Path(root or os.getenv("TERMINAL_BROKER_WORKDIR", "/work")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_limit = int(input_limit)
        self.artifact_limit = int(artifact_limit)
        self.session_ttl = min(MAX_SESSION_TTL, max(60, int(session_ttl)))
        self.sessions: dict[str, Session] = {}
        self.lock = asyncio.Lock()
        self.cleaner: asyncio.Task | None = None

    async def start(self):
        self.cleaner = asyncio.create_task(self._cleanup_loop())

    async def close(self):
        if self.cleaner:
            self.cleaner.cancel()
            await asyncio.gather(self.cleaner, return_exceptions=True)
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            await self._remove_session(session)

    async def _cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(30)
                now = time.monotonic()
                async with self.lock:
                    expired = [
                        session
                        for session in self.sessions.values()
                        if now >= session.expires_at or now - session.last_access > self.session_ttl
                    ]
                    for session in expired:
                        self.sessions.pop(session.session_id, None)
                for session in expired:
                    logger.info("expired terminal session %s", session.session_id[:8])
                    await self._remove_session(session)
        except asyncio.CancelledError:
            raise

    async def _remove_session(self, session: Session):
        for task in session.tasks.values():
            if task.runner and not task.runner.done():
                task.runner.cancel()
        await asyncio.gather(
            *[task.runner for task in session.tasks.values() if task.runner],
            return_exceptions=True,
        )
        shutil.rmtree(session.directory, ignore_errors=True)

    async def register(self, owner, *, ttl=None):
        owner = _handle(owner, kind="owner")
        requested_ttl = _bounded_number(ttl, self.session_ttl, 60, MAX_SESSION_TTL, integer=True)
        session_id = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
        bind_token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        session = Session(
            session_id,
            self.root / "sessions" / session_id,
            owner,
            expires_at=time.monotonic() + requested_ttl,
        )
        session.directory.mkdir(parents=True, exist_ok=True)
        session.bind_digest = hashlib.sha256(bind_token.encode()).hexdigest()
        async with self.lock:
            self.sessions[session_id] = session
        return {"sessionId": session_id, "bindToken": bind_token, "expiresInSec": requested_ttl}

    async def bind(self, session_id, bind_token, owner):
        session_id = _handle(session_id, kind="session")
        owner = _handle(owner, kind="owner")
        token = str(bind_token or "")
        if not token or len(token.encode()) > MAX_BIND_TOKEN_BYTES:
            raise BrokerError("invalid bind token")
        async with self.lock:
            session = self.sessions.get(session_id)
            if not session or session.owner != owner or session.expires_at <= time.monotonic():
                raise BrokerError("session not found or expired")
            digest = hashlib.sha256(token.encode()).hexdigest()
            if session.bound or not session.bind_digest or not hmac.compare_digest(session.bind_digest, digest):
                raise BrokerError("session binding rejected")
            session.bound = True
            session.bind_digest = None
            session.last_access = time.monotonic()
            return {"sessionId": session.session_id, "expiresInSec": max(0, int(session.expires_at - time.monotonic()))}

    async def session(self, session_id, owner):
        session_id = _handle(session_id, kind="session")
        owner = _handle(owner, kind="owner")
        async with self.lock:
            session = self.sessions.get(session_id)
            if session is None or not session.bound or session.expires_at <= time.monotonic():
                raise BrokerError("session not found or expired")
            if session.owner != owner:
                raise BrokerError("session owner mismatch")
            session.last_access = time.monotonic()
            return session

    async def close_session(self, session_id, owner):
        session_id = _handle(session_id, kind="session")
        owner = _handle(owner, kind="owner")
        async with self.lock:
            session = self.sessions.get(session_id)
            if session is None or session.owner != owner:
                raise BrokerError("session not found or expired")
            self.sessions.pop(session_id, None)
        await self._remove_session(session)
        return {"closed": True}

    async def add_input(self, session_id, owner, data, name, media_type):
        if not data:
            raise BrokerError("input is empty")
        if len(data) > self.input_limit:
            raise BrokerError("input exceeds size limit")
        session = await self.session(session_id, owner)
        if len(session.inputs) >= MAX_INPUTS_PER_SESSION:
            raise BrokerError("too many inputs in session")
        input_id = str(uuid.uuid4())
        input_dir = session.directory / "inputs"
        input_dir.mkdir(parents=True, exist_ok=True)
        path = input_dir / f"{input_id}.bin"
        await asyncio.to_thread(path.write_bytes, data)
        session.inputs[input_id] = path
        session.input_names[input_id] = _safe_name(name, "video")
        logger.info("accepted terminal input session=%s input=%s bytes=%s", session_id[:8], input_id[:8], len(data))
        return {
            "inputId": input_id,
            "name": session.input_names[input_id],
            "mediaType": _media_type(media_type),
            "size": len(data),
            "expiresInSec": self.session_ttl,
        }

    async def create_task(self, session_id, owner, operation, payload):
        session = await self.session(session_id, owner)
        if operation not in {"probe", "extract_frames", "extract_audio", "transcode"}:
            raise BrokerError("operation is not allowed")
        if len(session.tasks) >= MAX_TASKS_PER_SESSION:
            raise BrokerError("too many tasks in session")
        task_id = str(uuid.uuid4())
        task = Task(task_id, operation)
        session.tasks[task_id] = task
        task.runner = asyncio.create_task(self._run_task(session, task, payload))
        logger.info("created terminal task session=%s task=%s op=%s", session_id[:8], task_id[:8], operation)
        return {"taskId": task_id, "status": task.status}

    async def task_status(self, session_id, owner, task_id):
        session = await self.session(session_id, owner)
        task_id = _uuid_handle(task_id, kind="task")
        task = session.tasks.get(task_id)
        if not task:
            raise BrokerError("task not found")
        response = {"taskId": task.task_id, "status": task.status, "operation": task.operation}
        if task.result is not None:
            response["result"] = task.result
        if task.error:
            response["error"] = task.error
        return response

    async def cancel_task(self, session_id, owner, task_id):
        session = await self.session(session_id, owner)
        task_id = _uuid_handle(task_id, kind="task")
        task = session.tasks.get(task_id)
        if not task:
            raise BrokerError("task not found")
        if task.runner and not task.runner.done():
            task.runner.cancel()
            task.status = "cancelled"
        return {"ok": True, "taskId": task_id, "status": task.status}

    async def artifact(self, session_id, owner, artifact_id):
        session = await self.session(session_id, owner)
        artifact_id = _uuid_handle(artifact_id, kind="artifact")
        artifact = session.artifacts.get(artifact_id)
        if not artifact or not artifact.path.is_file():
            raise BrokerError("artifact not found or expired")
        if artifact.path.stat().st_size > self.artifact_limit:
            raise BrokerError("artifact exceeds size limit")
        return artifact

    async def _run_task(self, session: Session, task: Task, payload):
        task.status = "running"
        try:
            input_id = _uuid_handle(payload.get("inputId"), kind="input")
            input_path = session.inputs.get(input_id)
            if not input_path or not input_path.is_file():
                raise BrokerError("input not found or expired")
            if task.operation == "probe":
                task.result = await self._probe(input_path)
            elif task.operation == "extract_frames":
                task.result = await self._extract_frames(session, task, input_path, payload)
            elif task.operation == "extract_audio":
                task.result = await self._extract_audio(session, task, input_path, payload)
            elif task.operation == "transcode":
                task.result = await self._transcode(session, task, input_path, payload)
            task.status = "completed"
        except asyncio.CancelledError:
            task.status = "cancelled"
            raise
        except BrokerError as error:
            task.status = "failed"
            task.error = str(error)
        except Exception:
            task.status = "failed"
            task.error = "task failed"
            logger.exception("terminal task failed session=%s task=%s", session.session_id[:8], task.task_id[:8])

    async def _run_ffmpeg(self, args, *, timeout, output_limit=None):
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode != 0:
            logger.warning("media command failed rc=%s stderr=%s", process.returncode, stderr[-500:].decode(errors="replace"))
            raise BrokerError("media operation failed")
        if output_limit and len(stdout) > output_limit:
            raise BrokerError("media operation output is too large")
        return stdout

    async def _probe(self, input_path):
        raw = await self._run_ffmpeg(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(input_path)],
            timeout=30,
            output_limit=2 * 1024 * 1024,
        )
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise BrokerError("media metadata is invalid")
        return {"kind": "probe", "metadata": _safe_probe(parsed)}

    async def _extract_frames(self, session, task, input_path, payload):
        fps = _bounded_number(payload.get("fps"), 1.0, 0.05, 2.0)
        max_frames = _bounded_number(payload.get("maxFrames"), 12, 1, 24, integer=True)
        work = session.directory / "tasks" / task.task_id
        work.mkdir(parents=True, exist_ok=True)
        pattern = work / "frame-%03d.jpg"
        await self._run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
                "-vf", f"fps={fps},scale=1280:-2:force_original_aspect_ratio=decrease",
                "-frames:v", str(max_frames), "-q:v", "4", str(pattern),
            ],
            timeout=180,
        )
        frames = sorted(work.glob("frame-*.jpg"))
        if not frames:
            raise BrokerError("no video frames were produced")
        archive = work / "frames.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for frame in frames:
                bundle.write(frame, frame.name)
        return {"kind": "frames", "frameCount": len(frames), "artifact": self._register_artifact(session, task, archive, "frames.zip", "application/zip")}

    async def _extract_audio(self, session, task, input_path, payload):
        max_duration = _bounded_number(payload.get("maxDurationSec"), 300, 1, 900, integer=True)
        output = session.directory / "tasks" / task.task_id / "audio.m4a"
        output.parent.mkdir(parents=True, exist_ok=True)
        await self._run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
                "-t", str(max_duration), "-vn", "-ac", "2", "-ar", "48000",
                "-c:a", "aac", "-b:a", "128k", str(output),
            ],
            timeout=240,
        )
        return {"kind": "audio", "artifact": self._register_artifact(session, task, output, "audio.m4a", "audio/mp4")}

    async def _transcode(self, session, task, input_path, payload):
        max_duration = _bounded_number(payload.get("maxDurationSec"), 600, 1, 1800, integer=True)
        output = session.directory / "tasks" / task.task_id / "video.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        await self._run_ffmpeg(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path),
                "-t", str(max_duration), "-vf",
                "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output),
            ],
            timeout=600,
        )
        return {"kind": "video", "artifact": self._register_artifact(session, task, output, "video.mp4", "video/mp4")}

    def _register_artifact(self, session, task, path, name, media_type):
        size = path.stat().st_size
        if size <= 0 or size > self.artifact_limit:
            raise BrokerError("artifact exceeds size limit")
        if len(session.artifacts) >= MAX_ARTIFACTS_PER_SESSION:
            raise BrokerError("too many artifacts in session")
        artifact_id = str(uuid.uuid4())
        artifact = Artifact(artifact_id, path, _safe_name(name), media_type, size, task.task_id)
        session.artifacts[artifact_id] = artifact
        return {
            "artifactId": artifact_id,
            "name": artifact.name,
            "mediaType": artifact.media_type,
            "size": artifact.size,
        }


def _auth(request, secret):
    supplied = request.headers.get("Authorization", "")
    if not secret or supplied != f"Bearer {secret}":
        raise web.HTTPUnauthorized()


def _owner(request):
    return _handle(request.headers.get("X-Session-Owner", ""), kind="owner")


def create_app(state=None, secret=None):
    state = state or BrokerState()
    secret = secret if secret is not None else os.getenv("YUNYING_BROKER_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("YUNYING_BROKER_SECRET needs at least 32 characters")

    app = web.Application(client_max_size=DEFAULT_INPUT_BYTES + 1024 * 1024)

    async def on_startup(_app):
        await state.start()

    async def on_cleanup(_app):
        await state.close()

    async def health(_request):
        return web.json_response({"ok": True, "service": "terminal-broker"})

    async def add_input(request):
        _auth(request, secret)
        session_id = _handle(request.match_info["session"], kind="session")
        owner = _owner(request)
        length = request.content_length
        if length is not None and length > state.input_limit:
            raise web.HTTPRequestEntityTooLarge(max_size=state.input_limit, actual_size=length)
        data = await request.read()
        result = await state.add_input(
            session_id,
            owner,
            data,
            request.headers.get("X-Input-Name", "video"),
            request.headers.get("X-Input-Media-Type", "application/octet-stream"),
        )
        return web.json_response({"ok": True, **result})

    async def register_session(request):
        _auth(request, secret)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid json")
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="invalid registration")
        result = await state.register(payload.get("owner"), ttl=payload.get("ttlSec"))
        return web.json_response({"ok": True, **result})

    async def bind_session(request):
        _auth(request, secret)
        session_id = _handle(request.match_info["session"], kind="session")
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid json")
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="invalid binding")
        result = await state.bind(session_id, payload.get("bindToken"), payload.get("owner"))
        return web.json_response({"ok": True, **result})

    async def create_task(request):
        _auth(request, secret)
        session_id = _handle(request.match_info["session"], kind="session")
        owner = _owner(request)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="invalid json")
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="invalid task")
        operation = str(payload.pop("operation", ""))
        result = await state.create_task(session_id, owner, operation, payload)
        return web.json_response({"ok": True, **result})

    async def task_status(request):
        _auth(request, secret)
        result = await state.task_status(request.match_info["session"], _owner(request), request.match_info["task"])
        return web.json_response({"ok": True, **result})

    async def cancel_task(request):
        _auth(request, secret)
        result = await state.cancel_task(request.match_info["session"], _owner(request), request.match_info["task"])
        return web.json_response(result)

    async def close_session(request):
        _auth(request, secret)
        result = await state.close_session(request.match_info["session"], _owner(request))
        return web.json_response({"ok": True, **result})

    async def get_artifact(request):
        _auth(request, secret)
        artifact = await state.artifact(request.match_info["session"], _owner(request), request.match_info["artifact"])
        return web.FileResponse(
            artifact.path,
            headers={
                "X-Artifact-Name": artifact.name,
                "X-Artifact-Media-Type": artifact.media_type,
                "X-Artifact-Id": artifact.artifact_id,
            },
        )

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_post("/v1/sessions", register_session)
    app.router.add_post("/v1/sessions/{session}/bind", bind_session)
    app.router.add_delete("/v1/sessions/{session}", close_session)
    app.router.add_post("/v1/sessions/{session}/inputs", add_input)
    app.router.add_post("/v1/sessions/{session}/tasks", create_task)
    app.router.add_get("/v1/sessions/{session}/tasks/{task}", task_status)
    app.router.add_delete("/v1/sessions/{session}/tasks/{task}", cancel_task)
    app.router.add_get("/v1/sessions/{session}/artifacts/{artifact}", get_artifact)

    @web.middleware
    async def middleware_handler(request, handler):
        try:
            return await handler(request)
        except BrokerError as error:
            raise web.HTTPBadRequest(text=str(error))

    app.middlewares.append(middleware_handler)
    return app


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    state = BrokerState(
        input_limit=int(os.getenv("TERMINAL_BROKER_INPUT_LIMIT", str(DEFAULT_INPUT_BYTES))),
        artifact_limit=int(os.getenv("TERMINAL_BROKER_ARTIFACT_LIMIT", str(DEFAULT_ARTIFACT_BYTES))),
        session_ttl=int(os.getenv("TERMINAL_BROKER_SESSION_TTL", str(DEFAULT_SESSION_TTL))),
    )
    web.run_app(
        create_app(state),
        host=os.getenv("TERMINAL_BROKER_HOST", "0.0.0.0"),
        port=int(os.getenv("TERMINAL_BROKER_PORT", "8790")),
    )


if __name__ == "__main__":
    main()
