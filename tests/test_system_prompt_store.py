import asyncio
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from igngbot_v3.chat_service import ChatService, MessageContext
from igngbot_v3.system_prompt_store import SystemPromptStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PROMPT = (PROJECT_ROOT / "prompts" / "system.txt").read_text(encoding="utf-8").strip()


class FakePromptDB:
    def __init__(self, prompt, updated_at=None):
        self.prompt = prompt
        self.updated_at = updated_at
        self.calls = []

    def get_system_prompt_record(self, prompt_key):
        self.calls.append(prompt_key)
        return {
            "prompt_key": prompt_key,
            "prompt_text": self.prompt,
            "updated_at": self.updated_at,
        }


class SystemPromptStoreTest(unittest.TestCase):
    def make_store(self, temp_dir, db, *, bootstrap=CANONICAL_PROMPT):
        prompt_dir = Path(temp_dir) / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "system.txt").write_text(bootstrap, encoding="utf-8")
        config = SimpleNamespace(
            SYSTEM_PROMPT_CACHE_DIR=str(Path(temp_dir) / "cache"),
            LOCAL_STORAGE=str(Path(temp_dir) / "runtime"),
            PROMPT_DIR=str(prompt_dir),
            SYSTEM_PROMPT_SYNC_INTERVAL_SECONDS=0,
        )
        return SystemPromptStore(config, db)

    def test_canonical_prompt_contains_required_contract(self):
        valid, reason = SystemPromptStore.validate_prompt(CANONICAL_PROMPT)
        self.assertTrue(valid, reason)
        self.assertEqual(CANONICAL_PROMPT.count("# 固定人格：亲和"), 1)
        self.assertEqual(CANONICAL_PROMPT.count("# 道德与风险言论约束"), 1)

    def test_initialize_bootstraps_then_syncs_database_prompt_to_local_cache(self):
        database_prompt = CANONICAL_PROMPT + "\n\n# 数据库版本\n这是可编辑的数据库内容。"
        db = FakePromptDB(database_prompt, datetime(2026, 9, 10, 1, 2, 3))
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir, db)
            asyncio.run(store.initialize())

            self.assertEqual(db.calls, ["chat"])
            self.assertEqual(store.get("chat"), database_prompt)
            self.assertEqual(store.metadata()["source"], "database")
            self.assertEqual(
                store.metadata()["sha256"],
                SystemPromptStore.fingerprint(database_prompt),
            )
            self.assertEqual(store.cache_path.read_text(encoding="utf-8").strip(), database_prompt)
            metadata = json.loads(store.meta_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["db_updated_at"], "2026-09-10 01:02:03")

    def test_invalid_database_prompt_does_not_replace_last_valid_local_snapshot(self):
        db = FakePromptDB("# 不完整的提示词")
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(temp_dir, db)
            asyncio.run(store.initialize())

            self.assertEqual(store.get("chat"), CANONICAL_PROMPT)
            self.assertEqual(store.metadata()["source"], "bootstrap")
            self.assertFalse(asyncio.run(store.sync_once()))
            self.assertEqual(store.get("chat"), CANONICAL_PROMPT)

    def test_date_and_datetime_metadata_are_json_safe(self):
        self.assertEqual(
            SystemPromptStore._json_value(datetime(2026, 9, 10, 1, 2, 3)),
            "2026-09-10 01:02:03",
        )
        self.assertEqual(SystemPromptStore._json_value(date(2026, 9, 10)), "2026-09-10")


class LocalPromptCompositionTest(unittest.TestCase):
    def test_chat_system_prompt_is_composed_from_local_store_without_personality_or_risk_append(self):
        service = ChatService.__new__(ChatService)
        service.system_prompt_store = SimpleNamespace(
            get=lambda key: CANONICAL_PROMPT if key == "chat" else "",
            metadata=lambda: {"sha256": SystemPromptStore.fingerprint(CANONICAL_PROMPT), "source": "database"},
        )
        ctx = MessageContext(
            group_id=123,
            sender_id=456,
            sender_name="tester",
            msg_id="1001",
            rich_text="你觉得这个怎么样",
            direct_mention=True,
        )

        composed = service._build_system_prompt(ctx, {"is_chat_mode": True})

        self.assertTrue(composed.startswith(CANONICAL_PROMPT))
        self.assertEqual(composed.count("# 固定人格：亲和"), 1)
        self.assertEqual(composed.count("# 道德与风险言论约束"), 1)
        self.assertNotIn("当前性格", composed)
        self.assertNotIn("affinity_updates", composed)
        self.assertNotIn("RISK_SPEECH_CONSTRAINTS", composed)
        self.assertIn("当前消息已经写入数据库", composed)
        self.assertIn("本条消息显式 @了你或回复了你", composed)

    def test_personality_switching_is_not_exposed_as_a_chat_command(self):
        from igngbot_v3.main import App

        self.assertNotIn("/性格", App._HELP_DETAILS["聊天"])
        self.assertNotIn("性格", App._HELP_CATEGORIES["聊天"])

    def test_chat_service_fails_closed_when_local_prompt_is_missing(self):
        service = ChatService.__new__(ChatService)
        service.system_prompt_store = SimpleNamespace(get=lambda _key: "")
        service.db = SimpleNamespace(get_message_by_msg_id=lambda *_args: None)
        ctx = MessageContext(
            group_id=123,
            sender_id=456,
            sender_name="tester",
            msg_id="1001",
            rich_text="你在吗",
            direct_mention=True,
        )

        result = asyncio.run(service.maybe_reply(ctx))

        self.assertEqual(result, {"should_reply": False, "reason": "system_prompt_unavailable"})


if __name__ == "__main__":
    unittest.main()
