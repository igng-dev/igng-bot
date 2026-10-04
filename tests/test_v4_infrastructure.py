import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from PIL import Image
from igngbot_v4.settings import Settings, conversation_key, signed_conversation
from igngbot_v4.journal import ingress_identity
from igngbot_v4.views import message_view, row_images
from igngbot_v4.main import Infrastructure
from igngbot_v3.onebot_client import OneBotClient


def test_canonical_conversation_and_durable_event_dedup_identity():
    assert conversation_key("group", "001001") == "group:1001"
    assert signed_conversation("private:2001") == -2001
    with pytest.raises(ValueError):
        signed_conversation("group:001001")
    raw = {"post_type": "message", "message_type": "group", "group_id": 1001, "user_id": 2001, "message_id": -123}
    assert ingress_identity(raw) == ingress_identity({**raw, "message": "补偿消息"})
    assert ingress_identity({**raw, "post_type": "notice", "notice_type": "group_recall"}) != ingress_identity(raw)
    with pytest.raises(ValueError):
        ingress_identity({**raw, "message_id": None})


def test_private_recall_and_poke_notices_reach_optional_v4_callback():
    import json
    got = []
    client = OneBotClient(SimpleNamespace(), lambda _: None, notice_callback=got.append)
    for notice in [{"post_type": "notice", "notice_type": "friend_recall", "user_id": 2001, "message_id": 1},
                   {"post_type": "notice", "notice_type": "notify", "sub_type": "poke", "group_id": 1001, "user_id": 2001}]:
        client._on_message(None, json.dumps(notice))
    assert len(got) == 2


def test_history_recall_and_media_views_hide_paths_and_recalled_content(tmp_path):
    import json
    row = {"msg_id": "10", "sender_id": 2001, "message_content": "[图片][语音]", "plain_text_content": "图中文字和语音转写",
           "attachments_json": json.dumps([{"type": "image", "stored_path": "/private/file", "ocr_text": "图中文字"}]),
           "is_recalled": False}
    view = message_view(row)
    assert view["media"] == [{"type": "image", "ocr_text": "图中文字"}]
    assert view["plain"] == "图中文字和语音转写"
    row["is_recalled"] = True
    assert message_view(row)["text"] == "[消息已撤回]"
    assert message_view(row)["media"] == []
    assert row_images(row, None, tmp_path) == []


def test_image_reads_use_current_database_row_and_confined_attachment_root(tmp_path):
    import json
    root = tmp_path / "media"
    root.mkdir()
    image = root / "image.png"
    Image.new("RGB", (1600, 1200), "blue").save(image)
    outside = tmp_path / "other.png"
    Image.new("RGB", (10, 10)).save(outside)
    link = root / "escape.png"
    link.symlink_to(outside)
    storage = SimpleNamespace(resolve_path=lambda p: p)
    row = {"msg_id": "10", "attachments_json": json.dumps([{"type": "image", "stored_path": str(link)}, {"type": "image", "stored_path": str(image)}])}
    images = row_images(row, storage, root)
    assert len(images) == 1
    assert images[0]["mediaType"] == "image/webp"
    assert len(base64.b64decode(images[0]["data"])) > 10


class Response:
    status = 200
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
    async def __aenter__(self):
        if self.error:
            raise self.error
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
    def group_control(self, _event_id, _group, field, desired=None):
        self.last_control = (_event_id, _group, field, desired)
        return bool(desired) if desired is not None else True
    def finish_send(self, request_id, _status, result):
        self.rows[request_id] = result


def app_for_send(result):
    app = Infrastructure.__new__(Infrastructure)
    app.settings = Settings(str(uuid4()), groups=frozenset({"1001"}))
    app.config = SimpleNamespace(ONEBOT_HTTP_URL="http://fixture", ONEBOT_HTTP_TOKEN="", BOT_USER_ID=3001)
    app._send_lock = asyncio.Lock()
    app.journal = SendJournal()
    app.conn = MagicMock()
    app._test_social_paused = 0
    app._test_chat_mode = 1
    cur = app.conn.cursor.return_value.__enter__.return_value
    cur.execute.side_effect = lambda query, *_: setattr(cur, "_query", query)
    cur.fetchone.side_effect = lambda: ({"social_paused": app._test_social_paused,"is_chat_mode":app._test_chat_mode} if "group_configs" in cur._query else None)
    app.db = MagicMock()
    app.http = MagicMock()
    app.http.post.return_value = result
    return app


def test_tool_send_is_literal_segments_and_same_call_cannot_duplicate():
    async def scenario():
        app = app_for_send(Response({"status": "ok", "retcode": 0, "data": {"message_id": 9901}}))
        data = {"key": "group:1001", "requestId": "native-call:one", "message": "[CQ:at,qq=all] 只是文字"}
        first = await app.send(data)
        second = await app.send(data)
        assert first == second
        assert app.http.post.call_count == 1
        segments = app.http.post.call_args.kwargs["json"]["message"]
        assert segments == [{"type": "text", "data": {"text": data["message"]}}]
        assert app.db.insert_message.call_args.kwargs["message_source"] == "yunying_dsh"
    asyncio.run(scenario())


def test_uncertain_send_outcome_is_preserved_and_not_retried():
    async def scenario():
        app = app_for_send(Response(error=asyncio.TimeoutError()))
        data = {"key": "group:1001", "requestId": "native-call:timeout", "message": "你好"}
        assert (await app.send(data))["status"] == "unknown"
        assert (await app.send(data))["status"] == "unknown"
        assert app.http.post.call_count == 1
    asyncio.run(scenario())


def test_member_prompt_cannot_elevate_owner_command_permissions():
    async def scenario():
        app = app_for_send(Response({"status": "ok", "retcode": 0, "data": {"message_id": 9902}}))
        app.db.is_bot_admin.return_value = False
        result = await app.command({}, {"message_content": "/云萤暂停", "sender_id": 2001, "sender_role": "member"}, "group:1001", "command-1")
        assert result == {"commandHandled": True}
        app.db.conn.cursor.assert_not_called()
        assert "权限" in app.http.post.call_args.kwargs["json"]["message"][-1]["data"]["text"]
    asyncio.run(scenario())


def test_committed_raw_history_retry_still_reaches_dsh_without_reprocessing_media(monkeypatch):
    from igngbot_v4 import main as module
    async def must_not_repeat(*_):
        raise AssertionError('media should not be downloaded/transcribed twice')
    monkeypatch.setattr(module, 'persist_message', must_not_repeat)
    async def scenario():
        app = app_for_send(Response())
        app.db.get_message_by_msg_id.return_value = {'msg_id':'1234','sender_id':2001,'message_content':'你好','plain_text_content':'你好','is_self':False,'is_recalled':False}
        raw = {'post_type':'message','message_type':'group','group_id':1001,'user_id':2001,'self_id':3001,
               'message_id':1234,'message':[{'type':'at','data':{'qq':'3001'}},{'type':'text','data':{'text':'你好'}}],
               'sender':{'role':'member','nickname':'群友'}}
        result = await app.prepare({'conversation_key':'group:1001','event_id':'retry-raw','raw_event':raw})
        assert result['eventId'] == 'retry-raw' and result['atBot']
        assert result['text'] == '你好'
    asyncio.run(scenario())


def test_mentions_are_structured_and_restricted_to_current_group_members():
    async def scenario():
        app = app_for_send(Response({'status':'ok','retcode':0,'data':{'message_id':9904}}))
        app.db.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {'ok':1}
        await app.send({'key':'group:1001','requestId':'mention-1','message':'接着说','atUserId':'2001'})
        assert app.http.post.call_args.kwargs['json']['message'][0] == {'type':'at','data':{'qq':'2001'}}
        app.db.conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
        from aiohttp import web
        with pytest.raises(web.HTTPForbidden):
            await app.send({'key':'group:1001','requestId':'mention-denied','message':'你好','atUserId':'2999'})
    asyncio.run(scenario())


def test_social_pause_blocks_native_send_before_onebot_call():
    async def scenario():
        app = app_for_send(Response())
        app._test_social_paused = 1
        result = await app.send({"key": "group:1001", "requestId": "paused-call", "message": "不能发送"})
        assert not result["ok"]
        app.http.post.assert_not_called()
        assert not app.journal.rows
    asyncio.run(scenario())


def test_group_owner_commands_pause_and_resume_without_changing_legacy_chat_mode():
    async def scenario():
        app = app_for_send(Response({"status": "ok", "retcode": 0, "data": {"message_id": 9906}}))
        parsed = {"message_content": "/云萤暂停", "sender_id": 2001, "sender_role": "admin"}
        result = await app.command({}, parsed, "group:1001", "owner-pause")
        assert result["pause"] is True
        assert app.journal.last_control == ("owner-pause",1001,"social_paused",True)
        app._test_social_paused = 1
        result = await app.command({}, {**parsed, "message_content": "/云萤继续"}, "group:1001", "owner-toggle")
        assert result["pause"] is False
        assert app.journal.last_control == ("owner-toggle",1001,"social_paused",False)
    asyncio.run(scenario())


def test_group_configuration_changes_enter_durable_fifo_without_raw_qq_history():
    async def scenario():
        app = app_for_send(Response())
        app._group_modes = {}
        app._journal_signal = asyncio.Event()
        app.journal = MagicMock()
        app.journal.enqueue.return_value = ("group:1001", "control-1")
        await app.sync_group_modes()
        await app.sync_group_modes()
        assert app.journal.enqueue.call_count == 1
        raw = app.journal.enqueue.call_args.args[0]
        assert ingress_identity(raw)[0] == "group:1001"
        payload = await app.prepare({"raw_event": raw, "conversation_key": "group:1001", "event_id": "control-1"})
        assert payload["isConfiguration"] and payload["isSelf"] and payload["commandHandled"]
        app.db.insert_message.assert_not_called()
        app._test_social_paused = 1
        await app.sync_group_modes()
        assert app.journal.enqueue.call_count == 2
    asyncio.run(scenario())


def test_delivery_rechecks_current_group_permission_for_prepared_retry():
    async def scenario():
        app = app_for_send(Response({"ok": True}))
        app._stopping = False
        app.journal = MagicMock()
        app.journal.pending.return_value = {"event_id": "retry-permission", "conversation_key": "group:1001",
            "prepared_event": {"kind": "message", "pause": False}, "attempts": 0}
        app._test_social_paused = 1
        app.journal.finish.side_effect = lambda _: setattr(app, "_stopping", True)
        await app.deliver()
        assert app.http.post.call_args.kwargs["json"]["pause"] is True
        app.journal.finish.assert_called_once_with("retry-permission")
    asyncio.run(scenario())


def test_chat_mode_command_restores_authorized_participation_switch():
    async def scenario():
        app = app_for_send(Response({"status": "ok", "retcode": 0, "data": {"message_id": 9907}}))
        parsed = {"message_content": "/聊天模式", "sender_id": 2001, "sender_role": "admin"}
        result = await app.command({}, parsed, "group:1001", "restored-mode")
        assert result == {"commandHandled": True}
        assert app.journal.last_control == ("restored-mode",1001,"is_chat_mode",None)
        assert "自主" in app.http.post.call_args.kwargs["json"]["message"][-1]["data"]["text"]
    asyncio.run(scenario())


def test_call_only_mode_enforces_send_permission_at_infrastructure_boundary():
    async def scenario():
        app = app_for_send(Response({"status":"ok","retcode":0,"data":{"message_id":9999}}))
        app._test_chat_mode = 0
        data={"key":"group:1001","requestId":"uninvited","message":"不要自行发言"}
        assert not (await app.send(data))["ok"]
        app.http.post.assert_not_called()
        cur=app.conn.cursor.return_value.__enter__.return_value
        old=cur.fetchone.side_effect
        cur.fetchone.side_effect=lambda: {"ok":1} if "direct_event_id" in cur._query else old()
        assert (await app.send({**data,"requestId":"called","triggerEventId":"actual-call"}))["ok"]
        app._test_social_paused = 1
        assert not (await app.send({**data,"requestId":"paused","triggerEventId":"actual-call"}))["ok"]
        assert app.http.post.call_count==1
    asyncio.run(scenario())


def test_member_cannot_toggle_chat_mode():
    async def scenario():
        app=app_for_send(Response({"status":"ok","retcode":0,"data":{"message_id":9998}}))
        app.db.is_bot_admin.return_value=False
        await app.command({}, {"message_content":"/聊天模式 开启","sender_id":2001,"sender_role":"member"},"group:1001","untrusted-command")
        assert not hasattr(app.journal,"last_control")
        assert "权限" in app.http.post.call_args.kwargs["json"]["message"][-1]["data"]["text"]
    asyncio.run(scenario())
