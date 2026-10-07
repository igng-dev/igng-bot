"""Bot-side gateway for the isolated terminal broker.

Only this module talks to the broker. DSH receives opaque session/input/
artifact handles; filesystem paths, URLs, shell commands and arbitrary OneBot
actions never cross this boundary in either direction.
"""

import asyncio
import hmac
import json
import mimetypes
import re
import time
from pathlib import Path

from aiohttp import web

from .broker_client import BrokerClientError
from .settings import signed_conversation

HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{8,160}$")
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
MESSAGE_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
REPLY_ID_RE = re.compile(r"^-?[1-9][0-9]{0,19}$")
QQ_RE = re.compile(r"^[1-9][0-9]{0,19}$")
REQUEST_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")

MAX_INPUT_BYTES = 256 * 1024 * 1024
ATTACHMENT_TYPES = {"video", "file"}
OPERATIONS = {
    "/terminal/probe": ("probe", 45),
    "/terminal/frames": ("extract_frames", 200),
    "/terminal/audio": ("extract_audio", 280),
    "/terminal/transcode": ("transcode", 640),
}
LABELS = {"video": "[视频]", "image": "[图片]", "audio": "[音频]", "file": "[文件]"}


def _bounded_int(value, default, minimum, maximum, label):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise web.HTTPBadRequest(text=f"invalid {label}")
    number = int(value)
    if number < minimum or number > maximum:
        raise web.HTTPBadRequest(text=f"invalid {label}")
    return number


def _bounded_float(value, default, minimum, maximum, label):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise web.HTTPBadRequest(text=f"invalid {label}")
    number = float(value)
    if number < minimum or number > maximum:
        raise web.HTTPBadRequest(text=f"invalid {label}")
    return number


def safe_attachment_name(value, fallback="video.mp4"):
    name = NAME_RE.sub("_", str(value or "").strip()).strip("._")
    return name[:120] or fallback


def media_type_for(name, fallback="application/octet-stream"):
    guessed = mimetypes.guess_type(str(name))[0]
    return guessed or fallback


def collect_attachments(value, depth=0):
    """Find persisted video/file attachments in a stored message row."""
    found = []
    if depth > 12:
        return found
    if isinstance(value, list):
        for child in value:
            found.extend(collect_attachments(child, depth + 1))
    elif isinstance(value, dict):
        stored = str(value.get("stored_path") or "")
        kind = str(value.get("type") or "")
        if stored and kind in ATTACHMENT_TYPES:
            found.append({"type": kind, "storedPath": stored,
                          "name": str(value.get("name") or value.get("original_file") or "video.mp4")})
        for child in value.values():
            if isinstance(child, (list, dict)):
                found.extend(collect_attachments(child, depth + 1))
    return found


def confined_attachment(storage, root, stored_path):
    """Resolve a persisted path strictly inside the attachment root."""
    try:
        resolved = Path(storage.resolve_path(stored_path)).resolve()
        base = Path(root).resolve()
    except OSError:
        return None
    return resolved if resolved.is_relative_to(base) and resolved.is_file() else None


def artifact_segment(media_type, stored_path, name):
    """Fixed OneBot segment; the model never chooses the segment or its data."""
    media = str(media_type or "")
    reference = "file://" + str(stored_path)
    if media.startswith("video/"):
        return {"type": "video", "data": {"file": reference}}
    if media.startswith("image/"):
        return {"type": "image", "data": {"file": reference}}
    return {"type": "file", "data": {"file": reference, "name": safe_attachment_name(name, "artifact.bin")}}


def artifact_label(media_type):
    media = str(media_type or "")
    if media.startswith("video/"):
        return LABELS["video"]
    if media.startswith("image/"):
        return LABELS["image"]
    if media.startswith("audio/"):
        return LABELS["audio"]
    return LABELS["file"]


class TerminalGateway:
    """Validates DSH terminal calls and bridges bytes to the isolated broker."""

    def __init__(self, broker, *, authorize, db, storage, config, clock=time.monotonic):
        self.broker = broker
        self._authorize = authorize
        self.db = db
        self.storage = storage
        self.config = config
        self.clock = clock
        self._sessions = {}
        self._lock = asyncio.Lock()

    async def close(self):
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for item in sessions:
            try:
                await self.broker.close_session(item["sessionId"], item["owner"])
            except BrokerClientError:
                pass

    # -- identity ---------------------------------------------------------
    def _authorized(self, key):
        try:
            return bool(self._authorize(key))
        except Exception:
            return False

    def _identity(self, key):
        """DSH session id plus the persisted token; both live in yunying_sessions."""
        with self.db.conn.cursor() as cur:
            cur.execute("SELECT dsh_session_id, social_state FROM yunying_sessions WHERE conversation_key=%s", (key,))
            row = cur.fetchone()
        if not row:
            return None
        owner = str(row.get("dsh_session_id") or "")
        state = row.get("social_state")
        if isinstance(state, (str, bytes, bytearray)):
            try:
                state = json.loads(state)
            except ValueError:
                state = None
        token = str(state.get("agentToken") or "") if isinstance(state, dict) else ""
        if not HANDLE_RE.fullmatch(owner) or not 16 <= len(token) <= 128:
            return None
        return {"owner": owner, "agentToken": token}

    def _authenticate(self, key, data):
        if not self._authorized(key):
            raise web.HTTPForbidden(text="conversation denied")
        identity = self._identity(key)
        supplied = data.get("token")
        if not identity or not isinstance(supplied, str) or not hmac.compare_digest(supplied, identity["agentToken"]):
            raise web.HTTPForbidden(text="terminal session denied")
        return identity

    # -- broker sessions --------------------------------------------------
    async def _session(self, key, owner):
        now = self.clock()
        async with self._lock:
            cached = self._sessions.get(key)
            if cached and cached["owner"] == owner and cached["expiresAt"] - 60 > now:
                return cached["sessionId"]
            self._sessions.pop(key, None)
            try:
                created = await self.broker.register(owner)
                await self.broker.bind(created["sessionId"], created["bindToken"], owner)
            except BrokerClientError:
                raise web.HTTPBadGateway(text="terminal broker unavailable")
            self._sessions[key] = {"sessionId": created["sessionId"], "owner": owner,
                                   "expiresAt": now + max(60, int(created.get("expiresInSec") or 0))}
            return created["sessionId"]

    def _bound_session(self, key, owner, supplied):
        cached = self._sessions.get(key)
        text = str(supplied or "")
        if not cached or cached["owner"] != owner or cached["expiresAt"] <= self.clock() \
                or cached["sessionId"] != text or not HANDLE_RE.fullmatch(text):
            raise web.HTTPForbidden(text="terminal session expired; open the video again")
        return text

    # -- attachment lookup ------------------------------------------------
    def _attachment(self, key, message_id, index):
        row = self.db.get_message_by_msg_id(signed_conversation(key), message_id)
        if not row or row.get("is_recalled"):
            return None
        candidates = {}
        for source in (row.get("attachments_json"), row.get("message_structure")):
            value = source
            if isinstance(value, (str, bytes, bytearray)):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = None
            for item in collect_attachments(value):
                candidates.setdefault(item["storedPath"], item)
        ordered = list(candidates.values())
        if index >= len(ordered):
            return None
        chosen = ordered[index]
        path = confined_attachment(self.storage, self.config.MESSAGE_ROOT, chosen["storedPath"])
        if not path:
            return None
        name = safe_attachment_name(chosen["name"])
        return {"path": path, "name": name, "mediaType": media_type_for(name)}

    # -- capabilities -----------------------------------------------------
    async def open(self, data):
        key = str(data.get("key") or "")
        identity = self._authenticate(key, data)
        message_id = str(data.get("messageId") or "")
        if not MESSAGE_ID_RE.fullmatch(message_id):
            raise web.HTTPBadRequest(text="invalid message id")
        index = _bounded_int(data.get("attachmentIndex"), 0, 0, 9, "attachment index")
        attachment = await asyncio.to_thread(self._attachment, key, message_id, index)
        if not attachment:
            raise web.HTTPNotFound(text="video attachment not found")
        try:
            size = attachment["path"].stat().st_size
        except OSError:
            raise web.HTTPNotFound(text="video attachment not found")
        if size <= 0 or size > MAX_INPUT_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_INPUT_BYTES, actual_size=max(0, size))
        session_id = await self._session(key, identity["owner"])
        payload = await asyncio.to_thread(attachment["path"].read_bytes)
        try:
            result = await self.broker.add_input(session_id, identity["owner"], payload,
                name=attachment["name"], media_type=attachment["mediaType"])
        except BrokerClientError:
            raise web.HTTPBadGateway(text="terminal broker unavailable")
        return {"ok": True, "sessionId": session_id, "inputId": result.get("inputId"),
                "name": result.get("name"), "mediaType": result.get("mediaType"),
                "size": result.get("size"), "expiresInSec": result.get("expiresInSec")}

    async def operate(self, path, data):
        spec = OPERATIONS.get(str(path))
        if not spec:
            raise web.HTTPNotFound()
        operation, timeout = spec
        key = str(data.get("key") or "")
        identity = self._authenticate(key, data)
        session_id = self._bound_session(key, identity["owner"], data.get("sessionId"))
        input_id = str(data.get("inputId") or "")
        if not UUID_RE.fullmatch(input_id):
            raise web.HTTPBadRequest(text="invalid input handle")
        payload = {"inputId": input_id}
        if operation == "extract_frames":
            payload["fps"] = _bounded_float(data.get("fps"), 1.0, 0.05, 2.0, "fps")
            payload["maxFrames"] = _bounded_int(data.get("maxFrames"), 12, 1, 24, "maxFrames")
        elif operation == "extract_audio":
            payload["maxDurationSec"] = _bounded_int(data.get("maxDurationSec"), 300, 1, 900, "maxDurationSec")
        elif operation == "transcode":
            payload["maxDurationSec"] = _bounded_int(data.get("maxDurationSec"), 600, 1, 1800, "maxDurationSec")
        try:
            result = await self.broker.run_task(session_id, identity["owner"], operation, payload, timeout=timeout)
        except BrokerClientError as error:
            raise web.HTTPBadGateway(text=str(error) or "terminal operation failed")
        return {"ok": True, **result}

    async def send(self, data, *, deliver, gid):
        key = str(data.get("key") or "")
        identity = self._authenticate(key, data)
        session_id = self._bound_session(key, identity["owner"], data.get("sessionId"))
        artifact_id = str(data.get("artifactId") or "")
        if not UUID_RE.fullmatch(artifact_id):
            raise web.HTTPBadRequest(text="invalid artifact handle")
        request_id = str(data.get("requestId") or "")
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise web.HTTPBadRequest(text="invalid send identity")
        reply = data.get("replyToMessageId")
        if reply is not None and not REPLY_ID_RE.fullmatch(str(reply)):
            raise web.HTTPBadRequest(text="invalid reply id")
        if reply and not self.db.get_message_by_msg_id(gid, str(reply)):
            raise web.HTTPForbidden(text="reply is outside conversation")
        at_user = data.get("atUserId")
        if at_user is not None:
            if gid < 0 or not QQ_RE.fullmatch(str(at_user)):
                raise web.HTTPBadRequest(text="invalid mention target")
            with self.db.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM message_logs WHERE group_id=%s AND sender_id=%s LIMIT 1", (gid, str(at_user)))
                if not cur.fetchone():
                    raise web.HTTPForbidden(text="mention is outside conversation")

        async def prepared():
            try:
                artifact = await self.broker.download_artifact(session_id, identity["owner"], artifact_id)
            except BrokerClientError:
                raise web.HTTPBadGateway(text="attachment is unavailable")
            try:
                stored = await asyncio.to_thread(self.storage.store_bytes, gid, artifact["name"], artifact["data"])
            except Exception:
                raise web.HTTPBadGateway(text="attachment is unavailable")
            segment = artifact_segment(artifact["mediaType"], stored["stored_path"], artifact["name"])
            segments = ([{"type": "reply", "data": {"id": str(reply)}}] if reply else []) + \
                       ([{"type": "at", "data": {"qq": str(at_user)}}] if at_user else []) + [segment]
            return {"segments": segments, "text": artifact_label(artifact["mediaType"]),
                    "attachment": {"type": segment["type"], "stored_path": stored["stored_path"],
                                   "name": safe_attachment_name(artifact["name"], "artifact.bin"),
                                   "media_type": str(artifact["mediaType"]), "size": int(stored["size"])},
                    "reply": reply}

        return await deliver(key, gid, request_id,
                             {"artifactId": artifact_id, "reply": reply, "at": at_user}, prepared)
