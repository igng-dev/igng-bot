import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from igngbot_v3 import onebot_api
from igngbot_v3.message_parser import parse_message


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = payload if isinstance(payload, str) else json.dumps(payload)

    async def text(self):
        return self._body


class FakeRequest:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeRequest(self.response)


class FakeDB:
    def __init__(self):
        self.rows = []

    def insert_message(self, **kwargs):
        self.rows.append(kwargs)
        return True


def config():
    return SimpleNamespace(
        BOT_USER_ID=1000000001,
        ONEBOT_HTTP_URL="http://onebot.test",
        ONEBOT_HTTP_TOKEN="token",
    )


def test_group_text_records_bot_identity_and_source():
    db = FakeDB()
    session = FakeSession(FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 101}}))

    with patch.object(onebot_api.aiohttp, "ClientSession", return_value=session):
        result = asyncio.run(
            onebot_api.send_group_text(
                config(), 123, "聊天模式已开启", db=db, message_source="command"
            )
        )

    assert result is True
    assert len(db.rows) == 1
    row = db.rows[0]
    assert row["group_id"] == 123
    assert row["sender_id"] == 1000000001
    assert row["msg_id"] == "101"
    assert row["is_self"] is True
    assert row["message_source"] == "command"
    assert row["message_content"] == "聊天模式已开启"
    assert json.loads(row["message_structure"]) == [{"type": "text", "text": "聊天模式已开启"}]


def test_private_text_uses_negative_conversation_key_and_records_source():
    db = FakeDB()
    session = FakeSession(FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 202}}))

    with patch.object(onebot_api.aiohttp, "ClientSession", return_value=session):
        result = asyncio.run(
            onebot_api.send_private_text(
                config(), 456, "工单通知", db=db, message_source="notification"
            )
        )

    assert result is True
    assert db.rows[0]["group_id"] == -456
    assert db.rows[0]["sender_id"] == 1000000001
    assert db.rows[0]["message_source"] == "notification"


def test_http_failure_and_missing_message_id_do_not_create_rows():
    db = FakeDB()
    responses = [
        FakeResponse({"status": "failed", "retcode": 1}, status=200),
        FakeResponse({"status": "ok", "retcode": 0, "data": {}}, status=200),
    ]

    for response in responses:
        session = FakeSession(response)
        with patch.object(onebot_api.aiohttp, "ClientSession", return_value=session):
            asyncio.run(onebot_api.send_group_text(config(), 123, "不应落库", db=db))

    assert db.rows == []


def test_image_success_records_structured_attachment():
    db = FakeDB()
    session = FakeSession(FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 303}}))

    with patch.object(onebot_api.aiohttp, "ClientSession", return_value=session):
        result = asyncio.run(
            onebot_api.send_group_image(
                config(), 789, "https://img.test/a.png", "一张图片", db=db
            )
        )

    assert result is True
    row = db.rows[0]
    assert row["group_id"] == 789
    assert row["sender_id"] == 1000000001
    assert row["is_self"] is True
    assert row["message_source"] == "media"
    assert row["message_content"] == "[图片]"
    assert row["file_type"] == "image"
    assert json.loads(row["attachments_json"])[0]["type"] == "image"


def test_message_sent_events_are_self_events_and_private_events_use_negative_key():
    group = parse_message(
        {
            "post_type": "message_sent",
            "message_type": "group",
            "self_id": "1000000001",
            "user_id": "999999999",
            "group_id": "321",
            "message_id": 401,
            "message": "bot reply",
        }
    )
    assert group["group_id"] == 321
    assert group["sender_id"] == "1000000001"
    assert group["is_self"] is True
    assert group["message_source"] == "onebot_event"

    private = parse_message(
        {
            "post_type": "message_sent",
            "message_type": "private",
            "self_id": "1000000001",
            "user_id": "1000000001",
            "target_id": "654",
            "message_id": 402,
            "message": [{"type": "text", "data": {"text": "私聊回复"}}],
        }
    )
    assert private["group_id"] == -654
    assert private["sender_id"] == "1000000001"
    assert private["message_source"] == "onebot_event"

class FailingDB(FakeDB):
    def insert_message(self, **kwargs):
        raise RuntimeError("database unavailable")


def test_database_failure_does_not_turn_successful_qq_send_into_retry_failure():
    db = FailingDB()
    session = FakeSession(FakeResponse({"status": "ok", "retcode": 0, "data": {"message_id": 505}}))

    with patch.object(onebot_api.aiohttp, "ClientSession", return_value=session):
        result = asyncio.run(
            onebot_api.send_group_text(
                config(), 123, "已发送但数据库暂时不可用", db=db, message_source="command"
            )
        )

    assert result is True
    assert len(session.calls) == 1


class EventDB:
    def __init__(self):
        self.rows = {}
        self.inserts = []

    def ensure_group_exists(self, _group_id):
        return None

    def get_message_by_msg_id(self, group_id, msg_id):
        return self.rows.get((int(group_id), str(msg_id)))

    def insert_message(self, **kwargs):
        key = (int(kwargs["group_id"]), str(kwargs["msg_id"]))
        if key in self.rows:
            return False
        row = dict(kwargs)
        self.rows[key] = row
        self.inserts.append(row)
        return True


def test_message_sent_event_is_compensation_path_and_is_idempotent():
    from igngbot_v3.main import App

    db = EventDB()
    app = App.__new__(App)
    app.db = db
    event = {
        "post_type": "message_sent",
        "message_type": "group",
        "self_id": "1000000001",
        "group_id": "321",
        "message_id": 606,
        "user_id": "1000000001",
        "message": "补偿记录",
    }

    asyncio.run(app._handle_raw_message_async(event))
    asyncio.run(app._handle_raw_message_async(event))

    assert len(db.inserts) == 1
    assert db.inserts[0]["is_self"] is True
    assert db.inserts[0]["sender_id"] == "1000000001"
    assert db.inserts[0]["message_source"] == "onebot_event"


def test_message_without_id_is_ignored_before_database_insert():
    from igngbot_v3.main import App

    db = EventDB()
    app = App.__new__(App)
    app.db = db

    asyncio.run(
        app._handle_raw_message_async(
            {
                "post_type": "message_sent",
                "message_type": "group",
                "self_id": "1000000001",
                "group_id": 321,
                "message": "缺少消息ID",
            }
        )
    )

    assert db.inserts == []
