import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from igngbot_v3.chat_service import ChatService, MessageContext
from igngbot_v3.context_manager import (
    RECALLED_MESSAGE_PLACEHOLDER,
    format_context_message,
    message_tokens,
)
from igngbot_v3.db import DBHandler
from igngbot_v3.main import App
from igngbot_v3.onebot_client import OneBotClient


class RecallFixtureClientTest(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(ONEBOT_NAME="fixture", ONEBOT_WS_URL="ws://fixture")
        self.messages = []
        self.recalls = []
        self.client = OneBotClient(
            self.config,
            self.messages.append,
            self.recalls.append,
        )

    def test_message_and_recall_use_the_same_message_id(self):
        message = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 123,
            "message_id": 456,
            "user_id": 789,
            "message": "待撤回正文",
        }
        recall = {
            "post_type": "notice",
            "notice_type": "group_recall",
            "group_id": 123,
            "message_id": 456,
            "operator_id": 789,
            "user_id": 789,
        }

        self.client._on_message(None, json.dumps(message))
        self.client._on_message(None, json.dumps(recall))

        self.assertEqual(self.messages[0]["message_id"], self.recalls[0]["message_id"])
        self.assertEqual(self.recalls[0]["group_id"], 123)

    def test_malformed_recall_is_ignored_but_normal_messages_still_work(self):
        self.client._on_message(
            None,
            json.dumps({"post_type": "notice", "notice_type": "group_recall", "group_id": 123}),
        )
        self.client._on_message(
            None,
            json.dumps({"post_type": "message", "message_type": "group", "group_id": 123, "message_id": 1}),
        )

        self.assertEqual(self.recalls, [])
        self.assertEqual(len(self.messages), 1)


class AppRecallHandlerTest(unittest.TestCase):
    def test_handler_normalizes_event_and_passes_utc_time_to_db(self):
        calls = []
        app = App.__new__(App)
        app.db = SimpleNamespace(
            mark_message_recalled=lambda **kwargs: calls.append(kwargs) or {"status": "marked"}
        )

        result = asyncio.run(
            app._handle_recall_event_async(
                {
                    "post_type": "notice",
                    "notice_type": "group_recall",
                    "group_id": "123",
                    "message_id": 456,
                    "operator_id": "789",
                    "time": 1_700_000_000,
                }
            )
        )

        self.assertEqual(result["status"], "marked")
        self.assertEqual(calls[0]["group_id"], 123)
        self.assertEqual(calls[0]["msg_id"], "456")
        self.assertEqual(calls[0]["recall_operator_id"], 789)
        self.assertEqual(
            calls[0]["recalled_at"],
            datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).replace(tzinfo=None),
        )

    def test_invalid_operator_id_does_not_drop_valid_recall(self):
        calls = []
        app = App.__new__(App)
        app.db = SimpleNamespace(
            mark_message_recalled=lambda **kwargs: calls.append(kwargs) or {"status": "pending"}
        )

        result = asyncio.run(
            app._handle_recall_event_async(
                {
                    "group_id": 123,
                    "message_id": 456,
                    "operator_id": "bad-operator",
                }
            )
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(calls[0]["recall_operator_id"], None)

    def test_handler_does_not_touch_db_for_malformed_event(self):
        mark = Mock()
        app = App.__new__(App)
        app.db = SimpleNamespace(mark_message_recalled=mark)

        result = asyncio.run(
            app._handle_recall_event_async(
                {"post_type": "notice", "notice_type": "group_recall", "group_id": "not-a-number", "message_id": 1}
            )
        )

        self.assertIsNone(result)
        mark.assert_not_called()

    def test_recalled_message_is_not_used_for_auto_plus_one(self):
        app = App.__new__(App)
        app.db = SimpleNamespace(
            get_previous_non_self_message=lambda *_args: {
                "message_content": "机密正文",
                "plain_text_content": "机密正文",
                "is_recalled": 1,
            }
        )
        app._plus_one_states = {}
        app._plus_one_queued_decisions = {}

        result = asyncio.run(
            app._handle_auto_plus_one(
                {"group_id": 123, "msg_id": "2", "files": []},
                "机密正文",
            )
        )

        self.assertFalse(result)
        self.assertEqual(app._plus_one_states, {})


class RecallContextTest(unittest.TestCase):
    def test_recalled_message_is_replaced_and_original_fields_are_hidden(self):
        row = {
            "msg_id": "456",
            "message_content": "原始正文 SECRET",
            "plain_text_content": "OCR SECRET",
            "attachments_json": json.dumps([{"type": "image", "ocr_text": "IMAGE SECRET"}], ensure_ascii=False),
            "is_recalled": 1,
        }

        rendered = format_context_message(row)
        self.assertEqual(rendered, RECALLED_MESSAGE_PLACEHOLDER)
        self.assertNotIn("SECRET", rendered)
        self.assertLess(message_tokens(row), message_tokens({**row, "is_recalled": 0}))

    def test_chat_history_and_current_prompt_do_not_expose_recalled_body(self):
        service = ChatService.__new__(ChatService)
        ctx = MessageContext(
            group_id=123,
            sender_id=789,
            sender_name="tester",
            msg_id="456",
            rich_text="原始正文 SECRET",
            direct_mention=True,
        )
        history = [
            {
                "msg_id": "456",
                "sender_id": 789,
                "message_content": "原始正文 SECRET",
                "plain_text_content": "OCR SECRET",
                "is_recalled": 1,
            }
        ]

        history_text = service._format_history(history)
        prompt = service._build_user_prompt(ctx, history_text, history, None)

        self.assertIn(RECALLED_MESSAGE_PLACEHOLDER, history_text)
        self.assertIn(RECALLED_MESSAGE_PLACEHOLDER, prompt)
        self.assertNotIn("SECRET", history_text)
        self.assertNotIn("SECRET", prompt)

    def test_recalled_current_message_skips_chat_analysis(self):
        service = ChatService.__new__(ChatService)
        service.db = SimpleNamespace(
            get_message_by_msg_id=lambda *_args: {"is_recalled": 1}
        )
        ctx = MessageContext(
            group_id=123,
            sender_id=789,
            sender_name="tester",
            msg_id="456",
            rich_text="原始正文 SECRET",
            direct_mention=True,
        )

        result = asyncio.run(service.maybe_reply(ctx))

        self.assertFalse(result["should_reply"])
        self.assertEqual(result["reason"], "recalled")

    def test_recalled_history_attachments_are_not_sent_as_multimodal_images(self):
        service = ChatService.__new__(ChatService)
        service.config = SimpleNamespace(LLM_LOCAL_MULTIMODAL=True)
        service._load_image_as_data_url = Mock(return_value="data:image/webp;base64,not-used")
        ctx = MessageContext(
            group_id=123,
            sender_id=789,
            sender_name="tester",
            msg_id="999",
            rich_text="当前消息",
            direct_mention=True,
        )
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            history = [
                {
                    "msg_id": "456",
                    "is_recalled": 1,
                    "attachments_json": json.dumps([{"type": "image", "stored_path": image.name}]),
                }
            ]
            result = service._build_user_content(ctx, "prompt", history)

        self.assertEqual(result, "prompt")
        service._load_image_as_data_url.assert_not_called()

    def test_recalled_current_message_files_are_not_sent_as_multimodal_images(self):
        service = ChatService.__new__(ChatService)
        service.config = SimpleNamespace(LLM_LOCAL_MULTIMODAL=True)
        service.db = SimpleNamespace(
            get_message_by_msg_id=lambda *_args: {"is_recalled": 1}
        )
        service._load_image_as_data_url = Mock(return_value="data:image/webp;base64,not-used")
        ctx = MessageContext(
            group_id=123,
            sender_id=789,
            sender_name="tester",
            msg_id="999",
            rich_text="当前消息 SECRET",
            direct_mention=True,
            files=[{"type": "image", "stored_path": "/tmp/does-not-matter.png"}],
        )

        result = service._build_user_content(ctx, "prompt", [])

        self.assertEqual(result, "prompt")
        service._load_image_as_data_url.assert_not_called()

    def test_current_prompt_prefers_persisted_recalled_row_over_callback_body(self):
        service = ChatService.__new__(ChatService)
        service.db = SimpleNamespace(
            get_message_by_msg_id=lambda *_args: {
                "id": 99,
                "msg_id": "999",
                "sender_id": 789,
                "message_content": "原始正文 SECRET",
                "plain_text_content": "OCR SECRET",
                "is_recalled": 1,
            }
        )
        ctx = MessageContext(
            group_id=123,
            sender_id=789,
            sender_name="tester",
            msg_id="999",
            rich_text="回调正文 SECRET",
            direct_mention=True,
        )

        history = service._ensure_current_message_present([], ctx)
        prompt = service._build_user_prompt(ctx, service._format_history(history), history, None)

        self.assertEqual(history[-1]["_db_id"], 99)
        self.assertIn(RECALLED_MESSAGE_PLACEHOLDER, prompt)
        self.assertNotIn("SECRET", prompt)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self._result = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).lower().split())
        params = tuple(params or ())
        self.connection.queries.append((normalized, params))
        self._result = []
        self.rowcount = 0

        if normalized.startswith("insert into message_recall_events"):
            group_id, msg_id, operator_id, recalled_at = params
            current = self.connection.recall_events.get((group_id, msg_id))
            if current is None:
                self.connection.recall_events[(group_id, msg_id)] = {
                    "recall_operator_id": operator_id,
                    "recalled_at": recalled_at,
                    "processed_at": None,
                }
            else:
                if current["recall_operator_id"] is None:
                    current["recall_operator_id"] = operator_id
                if current["recalled_at"] is None:
                    current["recalled_at"] = recalled_at
                current["processed_at"] = None
        elif normalized.startswith("select id, is_recalled from message_logs"):
            group_id, msg_id = params
            self._result = [dict(row) for row in self.connection.messages.get((group_id, msg_id), [])]
        elif normalized.startswith("update message_logs set is_recalled"):
            recalled_at, operator_id, group_id, msg_id = params
            rows = self.connection.messages.get((group_id, msg_id), [])
            for row in rows:
                row["is_recalled"] = 1
                row.setdefault("recalled_at", recalled_at)
                row.setdefault("recall_operator_id", operator_id)
            self.rowcount = len(rows)
        elif normalized.startswith("update message_recall_events"):
            processed_at, group_id, msg_id = params
            event = self.connection.recall_events.get((group_id, msg_id))
            if event is not None:
                event["processed_at"] = event["processed_at"] or processed_at
                self.rowcount = 1
        elif normalized.startswith("update context_summaries"):
            self.rowcount = 1
        elif normalized.startswith("select recall_operator_id, recalled_at from message_recall_events"):
            group_id, msg_id = params
            event = self.connection.recall_events.get((group_id, msg_id))
            if event and event["processed_at"] is None:
                self._result = [dict(event)]
        elif normalized.startswith("insert into message_logs"):
            values = params
            group_id, sender_id = values[0], values[1]
            msg_id = values[7]
            is_recalled = values[15]
            self.connection.messages.setdefault((group_id, str(msg_id)), []).append(
                {
                    "id": len(self.connection.messages) + 1,
                    "is_recalled": is_recalled,
                    "recalled_at": values[16],
                    "recall_operator_id": values[17],
                    "sender_id": sender_id,
                }
            )
            self.rowcount = 1
        elif normalized.startswith("select * from message_logs"):
            group_id, msg_id = params
            self._result = [dict(row) for row in self.connection.messages.get((group_id, str(msg_id)), [])]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConnection:
    def __init__(self):
        self.messages = {}
        self.recall_events = {}
        self.queries = []
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=True):
        return None

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class RecallDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.db = DBHandler(SimpleNamespace())
        self.connection = FakeConnection()
        self.db._conn = self.connection

    def test_recall_first_then_message_insert_is_idempotent_and_group_scoped(self):
        pending = self.db.mark_message_recalled(
            100,
            "42",
            recall_operator_id=9,
            recalled_at=datetime(2026, 9, 8, 8, 0, 0),
        )
        self.assertEqual(pending["status"], "pending")

        self.db.insert_message(
            group_id=100,
            sender_id=7,
            message_content="已撤回正文",
            reply_to_msg_id=None,
            msg_id="42",
        )
        stored = self.connection.messages[(100, "42")][0]
        self.assertEqual(stored["is_recalled"], 1)
        self.assertEqual(stored["recall_operator_id"], 9)
        self.assertIsNotNone(self.connection.recall_events[(100, "42")]["processed_at"])

        self.connection.messages[(200, "42")] = [{"id": 2, "is_recalled": 0}]
        marked = self.db.mark_message_recalled(200, "42", recall_operator_id=8)
        self.assertEqual(marked["status"], "marked")
        self.assertEqual(self.connection.messages[(100, "42")][0]["is_recalled"], 1)
        self.assertEqual(self.connection.messages[(200, "42")][0]["is_recalled"], 1)

        select_params = [
            params
            for sql, params in self.connection.queries
            if sql.startswith("select id, is_recalled from message_logs")
        ]
        self.assertIn((200, "42"), select_params)
        self.assertNotIn((100, "42"), select_params[-1:])

    def test_repeated_recall_reports_already_recalled(self):
        self.connection.messages[(100, "42")] = [{"id": 1, "is_recalled": 1}]
        first = self.db.mark_message_recalled(100, "42", recall_operator_id=9)
        second = self.db.mark_message_recalled(100, "42", recall_operator_id=9)

        self.assertEqual(first["status"], "already_recalled")
        self.assertEqual(second["status"], "already_recalled")


if __name__ == "__main__":
    unittest.main()
