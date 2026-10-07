"""Focused security and lifecycle tests for the isolated terminal broker."""

import asyncio
import json
import time
import uuid

import pytest
from aiohttp.test_utils import TestClient, TestServer

from igngbot_v4.broker_client import BrokerClient, BrokerClientError
from terminal_broker.server import BrokerError, BrokerState, Task, _safe_probe, create_app

SECRET = "broker" + "s" * 40
OWNER = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


async def bound_session(state, owner=OWNER):
    created = await state.register(owner)
    await state.bind(created["sessionId"], created["bindToken"], owner)
    return created["sessionId"]


def test_registration_binding_and_owner_checks_are_one_time(tmp_path):
    async def scenario():
        state = BrokerState(tmp_path)
        created = await state.register(OWNER)
        with pytest.raises(BrokerError):
            await state.session(created["sessionId"], OWNER)  # unbound sessions are unusable
        await state.bind(created["sessionId"], created["bindToken"], OWNER)
        with pytest.raises(BrokerError):
            await state.bind(created["sessionId"], created["bindToken"], OWNER)  # one-time token
        with pytest.raises(BrokerError):
            await state.session(created["sessionId"], OTHER)  # owner mismatch
        session = await state.session(created["sessionId"], OWNER)
        assert session.session_id == created["sessionId"]
        state.sessions[created["sessionId"]].expires_at = time.monotonic() - 1
        with pytest.raises(BrokerError):
            await state.session(created["sessionId"], OWNER)
        await state.close()
    asyncio.run(scenario())


def test_input_limits_and_name_sanitization(tmp_path):
    async def scenario():
        state = BrokerState(tmp_path, input_limit=4)
        session_id = await bound_session(state)
        with pytest.raises(BrokerError):
            await state.add_input(session_id, OWNER, b"", "clip.mp4", "video/mp4")
        with pytest.raises(BrokerError):
            await state.add_input(session_id, OWNER, b"12345", "clip.mp4", "video/mp4")
        result = await state.add_input(session_id, OWNER, b"1234", "../clip.mp4", "video/mp4; charset=x")
        assert result["size"] == 4 and result["name"] == "clip.mp4"
        assert result["mediaType"] == "application/octet-stream"
        await state.close()
    asyncio.run(scenario())


def test_probe_metadata_is_path_free():
    raw = {"format": {"filename": "/etc/passwd", "duration": "1.0", "format_name": "mov,mp4,m4a"},
           "streams": [{"codec_type": "video", "width": 1920, "extradata": "AAAA"}]}
    safe = _safe_probe(raw)
    assert "filename" not in safe["format"] and "extradata" not in safe["streams"][0]
    assert "/etc/passwd" not in json.dumps(safe)
    assert safe["format"]["duration"] == "1.0"


def test_broker_http_requires_bearer_and_registered_owner(tmp_path):
    async def scenario():
        state = BrokerState(tmp_path)
        client = TestClient(TestServer(create_app(state, secret=SECRET)))
        await client.start_server()
        try:
            assert (await client.get("/health")).status == 200
            assert (await client.post("/v1/sessions", json={"owner": OWNER})).status == 401
            auth = {"Authorization": f"Bearer {SECRET}"}
            created = await (await client.post("/v1/sessions", json={"owner": OWNER}, headers=auth)).json()
            session_id = created["sessionId"]
            bind = {"bindToken": created["bindToken"], "owner": OWNER}
            assert (await client.post(f"/v1/sessions/{session_id}/bind", json=bind, headers=auth)).status == 200
            assert (await client.post(f"/v1/sessions/{session_id}/bind", json=bind, headers=auth)).status == 400
            missing = await client.post(f"/v1/sessions/{session_id}/inputs", data=b"bytes", headers=auth)
            assert missing.status == 400
            wrong = await client.post(f"/v1/sessions/{session_id}/inputs", data=b"bytes",
                                      headers={**auth, "X-Session-Owner": OTHER})
            assert wrong.status == 400
            uploaded = await client.post(f"/v1/sessions/{session_id}/inputs", data=b"bytes", headers={
                **auth, "X-Session-Owner": OWNER, "X-Input-Name": "../weird name.mp4",
                "X-Input-Media-Type": "video/mp4; charset=x"})
            payload = await uploaded.json()
            assert uploaded.status == 200
            assert payload["name"] == "weird_name.mp4"
            assert payload["mediaType"] == "application/octet-stream"
            closed = await client.delete(f"/v1/sessions/{session_id}", headers={**auth, "X-Session-Owner": OWNER})
            assert closed.status == 200
            gone = await client.post(f"/v1/sessions/{session_id}/inputs", data=b"bytes",
                                     headers={**auth, "X-Session-Owner": OWNER})
            assert gone.status == 400
        finally:
            await client.close()
    asyncio.run(scenario())


def test_broker_client_round_trip_and_task_cancellation(tmp_path, monkeypatch):
    async def scenario():
        state = BrokerState(tmp_path)
        client = TestClient(TestServer(create_app(state, secret=SECRET)))
        await client.start_server()
        broker = BrokerClient(str(client.server.make_url("")).rstrip("/"), SECRET)
        try:
            created = await broker.register(OWNER)
            await broker.bind(created["sessionId"], created["bindToken"], OWNER)
            session_id = created["sessionId"]
            uploaded = await broker.add_input(session_id, OWNER, b"0123456789", name="clip.mp4", media_type="video/mp4")
            assert uploaded["size"] == 10 and uploaded["inputId"]

            async def fake_probe(_self, input_path):
                assert input_path.is_file()
                return {"kind": "probe", "metadata": {"duration": "1.0"}}

            monkeypatch.setattr(BrokerState, "_probe", fake_probe)
            result = await broker.run_task(session_id, OWNER, "probe", {"inputId": uploaded["inputId"]}, timeout=10)
            assert result == {"kind": "probe", "metadata": {"duration": "1.0"}}

            session = await state.session(session_id, OWNER)
            artifact_path = session.directory / "clip.mp4"
            artifact_path.write_bytes(b"artifact-bytes")
            artifact = state._register_artifact(session, Task(str(uuid.uuid4()), "probe"),
                                                artifact_path, "clip.mp4", "video/mp4")
            download = await broker.download_artifact(session_id, OWNER, artifact["artifactId"])
            assert download["data"] == b"artifact-bytes" and download["mediaType"] == "video/mp4"
            with pytest.raises(BrokerClientError):
                await broker.download_artifact(session_id, OWNER, str(uuid.uuid4()))

            async def slow_probe(_self, _input_path):
                await asyncio.sleep(30)

            monkeypatch.setattr(BrokerState, "_probe", slow_probe)
            with pytest.raises(BrokerClientError):
                await broker.run_task(session_id, OWNER, "probe", {"inputId": uploaded["inputId"]}, timeout=1)
            await asyncio.sleep(0.3)
            assert any(task.status == "cancelled" for task in session.tasks.values())
        finally:
            await client.close()
    asyncio.run(scenario())
