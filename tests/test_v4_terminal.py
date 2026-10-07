"""Focused tests for the bot-side terminal gateway (no real broker or NAS)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from igngbot_v4.broker_client import BrokerClientError
from igngbot_v4.main import Infrastructure
from igngbot_v4.settings import Settings
from igngbot_v4.terminal import TerminalGateway, collect_attachments

TOKEN = "agent-token-0123456789abcdef"
OWNER = "11111111-2222-3333-4444-555555555555"
SESSION = "session-" + "s" * 24
INPUT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ARTIFACT = "12345678-1234-1234-1234-123456789abc"


class FakeBroker:
    def __init__(self):
        self.registrations = []
        self.bindings = []
        self.inputs = []
        self.tasks = []
        self.downloads = []
        self.closed = []

    async def register(self, owner, *, ttl=None):
        self.registrations.append(owner)
        return {"sessionId": SESSION, "bindToken": "bind-" + "b" * 24, "expiresInSec": 1800}

    async def bind(self, session_id, bind_token, owner):
        self.bindings.append((session_id, bind_token, owner))

    async def close_session(self, session_id, owner):
        self.closed.append((session_id, owner))
        return True

    async def add_input(self, session_id, owner, data, *, name, media_type):
        self.inputs.append({"sessionId": session_id, "owner": owner, "data": bytes(data),
                            "name": name, "mediaType": media_type})
        return {"inputId": INPUT, "name": name, "mediaType": media_type,
                "size": len(data), "expiresInSec": 1800}

    async def run_task(self, session_id, owner, operation, payload, *, timeout=600):
        self.tasks.append({"sessionId": session_id, "owner": owner, "operation": operation,
                           "payload": payload, "timeout": timeout})
        return {"kind": operation, "artifact": {"artifactId": ARTIFACT, "name": "out.mp4",
                                                "mediaType": "video/mp4", "size": 14}}

    async def download_artifact(self, session_id, owner, artifact_id):
        if artifact_id != ARTIFACT:
            raise BrokerClientError("broker artifact lookup failed")
        self.downloads.append(artifact_id)
        return {"data": b"artifact-bytes", "name": "clip.mp4", "mediaType": "video/mp4",
                "artifactId": artifact_id, "size": 14}


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.query = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, _args=None):
        self.query = query

    def fetchone(self):
        if "yunying_sessions" in self.query:
            return self.row
        if "message_logs" in self.query:
            return {"ok": 1}
        return None


class FakeDB:
    def __init__(self, row, messages):
        self.row = row
        self.messages = messages

    @property
    def conn(self):
        return SimpleNamespace(cursor=lambda: FakeCursor(self.row))

    def get_message_by_msg_id(self, gid, message_id):
        return self.messages.get((gid, str(message_id)))


class FakeStorage:
    def __init__(self, root):
        self.root = root

    def resolve_path(self, stored):
        return str(stored)

    def store_bytes(self, gid, name, data):
        path = self.root / f"{gid}-{name}"
        path.write_bytes(bytes(data))
        return {"stored_path": str(path), "size": len(data)}


def identity_row(token=TOKEN):
    return {"dsh_session_id": OWNER, "social_state": json.dumps({"agentToken": token})}


def gateway_for(root, *, row=None, messages=None, authorize=None):
    broker = FakeBroker()
    db = FakeDB(row if row is not None else identity_row(), messages or {})
    gateway = TerminalGateway(broker, authorize=authorize or (lambda key: key == "group:1001"), db=db,
                              storage=FakeStorage(root), config=SimpleNamespace(MESSAGE_ROOT=str(root)))
    return gateway, broker, db


def video_message(path):
    return {"attachments_json": json.dumps([{"type": "video", "stored_path": str(path), "name": "clip.mp4"}])}


def test_open_requires_allowlist_and_persisted_agent_token(tmp_path):
    async def scenario():
        gateway, broker, _ = gateway_for(tmp_path)
        with pytest.raises(web.HTTPForbidden):
            await gateway.open({"key": "group:2002", "token": TOKEN, "messageId": "10"})
        with pytest.raises(web.HTTPForbidden):
            await gateway.open({"key": "group:1001", "token": "wrong-token-0123456789", "messageId": "10"})
        with pytest.raises(web.HTTPNotFound):
            await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10"})
        assert broker.registrations == []
    asyncio.run(scenario())


def test_open_streams_confined_video_and_reuses_one_broker_session(tmp_path):
    async def scenario():
        root = tmp_path / "media"
        root.mkdir()
        video = root / "clip.mp4"
        video.write_bytes(b"video-bytes")
        outside = tmp_path / "secret.mp4"
        outside.write_bytes(b"secret")
        message = {"attachments_json": json.dumps([
            {"type": "image", "stored_path": str(video)},
            {"type": "video", "stored_path": str(video), "name": "../clip.mp4"},
            {"type": "video", "stored_path": str(outside), "name": "secret.mp4"},
        ])}
        gateway, broker, _ = gateway_for(root, messages={(1001, "10"): message})
        first = await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10"})
        second = await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10"})
        assert first["sessionId"] == second["sessionId"] == SESSION
        assert len(broker.registrations) == 1 and len(broker.bindings) == 1
        assert len(broker.inputs) == 2
        assert broker.inputs[0]["data"] == b"video-bytes"
        assert broker.inputs[0]["name"] == "clip.mp4" and broker.inputs[0]["mediaType"] == "video/mp4"
        with pytest.raises(web.HTTPNotFound):
            await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10", "attachmentIndex": 1})
        assert len(broker.inputs) == 2
        await gateway.close()
        assert broker.closed == [(SESSION, OWNER)]
    asyncio.run(scenario())


def test_operate_bounds_parameters_and_binds_handles_to_the_conversation(tmp_path):
    async def scenario():
        root = tmp_path / "media"
        root.mkdir()
        video = root / "clip.mp4"
        video.write_bytes(b"video-bytes")
        gateway, broker, _ = gateway_for(root, messages={(1001, "10"): video_message(video)})
        opened = await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10"})
        with pytest.raises(web.HTTPForbidden):
            await gateway.operate("/terminal/probe", {"key": "group:1001", "token": TOKEN,
                                                      "sessionId": "session-" + "x" * 24, "inputId": INPUT})
        with pytest.raises(web.HTTPBadRequest):
            await gateway.operate("/terminal/frames", {"key": "group:1001", "token": TOKEN,
                                                       "sessionId": opened["sessionId"], "inputId": INPUT, "fps": 99})
        with pytest.raises(web.HTTPBadRequest):
            await gateway.operate("/terminal/probe", {"key": "group:1001", "token": TOKEN,
                                                      "sessionId": opened["sessionId"], "inputId": "not-a-uuid"})
        result = await gateway.operate("/terminal/frames", {
            "key": "group:1001", "token": TOKEN, "sessionId": opened["sessionId"], "inputId": INPUT,
            "fps": 0.5, "maxFrames": 4, "storedPath": "/etc/passwd", "command": "rm -rf /"})
        assert result["ok"] is True
        task = broker.tasks[-1]
        assert task["operation"] == "extract_frames"
        assert task["payload"] == {"inputId": INPUT, "fps": 0.5, "maxFrames": 4}
        assert "storedPath" not in task["payload"] and "command" not in task["payload"]
    asyncio.run(scenario())


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.payload


class SendJournal:
    def __init__(self):
        self.rows = {}

    def begin_send(self, request_id, _key, _payload):
        if request_id in self.rows:
            return False, self.rows[request_id]
        self.rows[request_id] = {"ok": False, "status": "unknown"}
        return True, None

    def finish_send(self, request_id, _status, result):
        self.rows[request_id] = result


def infrastructure_for():
    app = Infrastructure.__new__(Infrastructure)
    app._send_lock = asyncio.Lock()
    app.journal = SendJournal()
    app.http = MagicMock()
    app.http.post.return_value = Response({"status": "ok", "retcode": 0, "data": {"message_id": 9901}})
    app.config = SimpleNamespace(ONEBOT_HTTP_URL="http://fixture", ONEBOT_HTTP_TOKEN="", BOT_USER_ID=3001)
    app.db = MagicMock()
    return app


def test_send_delivers_video_once_with_fixed_file_segment_and_history(tmp_path):
    async def scenario():
        root = tmp_path / "media"
        root.mkdir()
        video = root / "clip.mp4"
        video.write_bytes(b"video-bytes")
        gateway, broker, _ = gateway_for(root, messages={(1001, "10"): video_message(video)})
        opened = await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10"})
        app = infrastructure_for()
        data = {"key": "group:1001", "token": TOKEN, "sessionId": opened["sessionId"],
                "artifactId": ARTIFACT, "requestId": "call:artifact:1"}
        first = await gateway.send(data, deliver=app._deliver, gid=1001)
        second = await gateway.send(data, deliver=app._deliver, gid=1001)
        assert first["ok"] is True and second == first
        assert len(broker.downloads) == 1
        assert app.http.post.call_count == 1
        sent = app.http.post.call_args.kwargs["json"]
        assert sent["group_id"] == 1001
        assert sent["message"] == [{"type": "video", "data": {"file": "file://" + str(root / "1001-clip.mp4")}}]
        history = app.db.insert_message.call_args.kwargs
        assert history["message_source"] == "yunying_dsh"
        attachment = json.loads(history["attachments_json"])[0]
        assert attachment["type"] == "video" and attachment["stored_path"].endswith("1001-clip.mp4")
    asyncio.run(scenario())


def test_send_reports_missing_artifact_without_touching_onebot(tmp_path):
    async def scenario():
        root = tmp_path / "media"
        root.mkdir()
        video = root / "clip.mp4"
        video.write_bytes(b"video-bytes")
        gateway, broker, _ = gateway_for(root, messages={(1001, "10"): video_message(video)})
        opened = await gateway.open({"key": "group:1001", "token": TOKEN, "messageId": "10"})
        app = infrastructure_for()
        data = {"key": "group:1001", "token": TOKEN, "sessionId": opened["sessionId"],
                "artifactId": "00000000-0000-0000-0000-000000000000", "requestId": "call:artifact:2"}
        result = await gateway.send(data, deliver=app._deliver, gid=1001)
        assert result["ok"] is False and result["status"] == "failed"
        app.http.post.assert_not_called()
        gateway._sessions.clear()
        with pytest.raises(web.HTTPForbidden):
            await gateway.send(data, deliver=app._deliver, gid=1001)
    asyncio.run(scenario())


def test_collect_attachments_ignores_non_media_and_pathless_entries():
    found = collect_attachments([
        {"type": "video", "stored_path": "/data/v.mp4", "name": "v.mp4"},
        {"type": "image", "stored_path": "/data/i.webp"},
        {"type": "file", "stored_path": "/data/f.zip"},
        {"type": "file"},
    ])
    assert [item["storedPath"] for item in found] == ["/data/v.mp4", "/data/f.zip"]


def test_broker_settings_require_a_secret_when_url_is_configured(monkeypatch):
    monkeypatch.setenv("YUNYING_INTERNAL_SECRET", "i" * 32)
    monkeypatch.delenv("YUNYING_BROKER_URL", raising=False)
    monkeypatch.delenv("YUNYING_BROKER_SECRET", raising=False)
    settings = Settings.from_env()
    assert settings.broker_url == "" and settings.broker_secret == ""
    monkeypatch.setenv("YUNYING_BROKER_URL", "http://broker:8790")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("YUNYING_BROKER_SECRET", "b" * 32)
    settings = Settings.from_env()
    assert settings.broker_url == "http://broker:8790" and settings.broker_secret == "b" * 32
