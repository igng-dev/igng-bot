import asyncio
import json
import unittest
from types import SimpleNamespace

from igngbot_v3 import call_log_db
from igngbot_v3.call_log_db import _extract_tokens, mirror_call_to_site


class FakeCursor:
    def __init__(self, recorder):
        self.recorder = recorder
        self.lastrowid = None
        self.selected = None

    async def execute(self, sql, params=None):
        if sql.strip().upper().startswith("SELECT GET_LOCK"):
            self.selected = (1,)
        elif sql.strip().upper().startswith("SELECT"):
            self.selected = None
        if sql.strip().upper().startswith("INSERT"):
            self.recorder.append((sql, params))
        if sql.strip().upper().startswith("INSERT INTO AI_JOBS"):
            self.lastrowid = 987

    async def fetchone(self):
        return self.selected

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, recorder):
        self.recorder = recorder

    async def begin(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    def cursor(self):
        return FakeCursor(self.recorder)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAcquire:
    def __init__(self, recorder):
        self.recorder = recorder

    async def __aenter__(self):
        return FakeConn(self.recorder)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, recorder):
        self.recorder = recorder

    def acquire(self):
        return FakeAcquire(self.recorder)


class ExtractTokensTest(unittest.TestCase):
    def test_openai_style_with_cache_details(self):
        usage = {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
        self.assertEqual(_extract_tokens(usage), (120, 30, 150, 40))

    def test_deepseek_style_cache_hit(self):
        usage = {
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "total_tokens": 60,
            "prompt_cache_hit_tokens": 32,
        }
        self.assertEqual(_extract_tokens(usage), (50, 10, 60, 32))

    def test_native_dsh_disjoint_cached_input_is_mapped_to_site_total(self):
        self.assertEqual(_extract_tokens({"inputTokens":30,"outputTokens":10,"cacheReadTokens":20,"cacheWriteTokens":5}), (55,10,65,25))

    def test_missing_usage(self):
        self.assertEqual(_extract_tokens(None), (0, 0, 0, 0))


class MirrorCallToSiteTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_writes_job_and_attempt_rows(self):
        recorder = []

        async def fake_pool():
            return FakePool(recorder)

        original = call_log_db._get_site_pool
        call_log_db._get_site_pool = fake_pool
        try:
            self._run(
                mirror_call_to_site(
                    call_log_id=42,
                    group_id="123",
                    sender_id="10001",
                    sender_name="tester",
                    message_text="hello",
                    call_type="chat",
                    model="Qwen3-4B",
                    system_prompt="sys",
                    user_prompt="user",
                    response_content='{"should_reply": true}',
                    token_usage={
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "prompt_tokens_details": {"cached_tokens": 8},
                    },
                    duration_ms=1500,
                    success=True,
                    error_message="",
                )
            )
        finally:
            call_log_db._get_site_pool = original

        self.assertEqual(len(recorder), 2)
        job_sql, job_params = recorder[0]
        attempt_sql, attempt_params = recorder[1]
        self.assertIn("INSERT INTO ai_jobs", job_sql)
        self.assertIn("INSERT INTO ai_job_attempts", attempt_sql)
        self.assertEqual(job_params[0], "igng-bot")
        self.assertEqual(job_params[1], "chat")
        self.assertEqual(job_params[2], "42")
        self.assertEqual(job_params[3], "BOT")
        self.assertIsNone(job_params[4])
        self.assertEqual(job_params[8], "success")
        strategy = json.loads(job_params[5])
        self.assertEqual(strategy["bot_call_log_id"], 42)
        self.assertEqual(strategy["sender_id"], "10001")
        self.assertEqual(job_params[10:14], (100, 20, 120, 8))
        self.assertEqual(attempt_params[0], 987)
        self.assertEqual(attempt_params[2], "Qwen3-4B")
        self.assertEqual(attempt_params[8], 1)
        self.assertEqual(attempt_params[14], '{"should_reply": true}')

    def test_failure_recorded_not_raised(self):
        class BoomPool:
            def acquire(self):
                raise RuntimeError("site db down")

        async def fake_pool():
            return BoomPool()

        original = call_log_db._get_site_pool
        call_log_db._get_site_pool = fake_pool
        try:
            self._run(
                mirror_call_to_site(
                    call_log_id=43,
                    call_type="chat",
                    success=False,
                    error_message="boom",
                )
            )
        finally:
            call_log_db._get_site_pool = original

    def test_disabled_skips_all_writes(self):
        recorder = []
        original_enabled = call_log_db.Config.SITE_AI_RECORDS_ENABLED
        original_pool = call_log_db._get_site_pool
        call_log_db.Config.SITE_AI_RECORDS_ENABLED = False
        call_log_db._get_site_pool = lambda: (_ for _ in ()).throw(
            AssertionError("pool must not be used when disabled")
        )
        try:
            self._run(mirror_call_to_site(call_log_id=44))
        finally:
            call_log_db.Config.SITE_AI_RECORDS_ENABLED = original_enabled
            call_log_db._get_site_pool = original_pool
        self.assertEqual(recorder, [])

    def test_missing_call_log_id_skips(self):
        async def fake_pool():
            raise AssertionError("pool must not be acquired without a call log id")

        original = call_log_db._get_site_pool
        call_log_db._get_site_pool = fake_pool
        try:
            self._run(mirror_call_to_site(call_log_id=None))
        finally:
            call_log_db._get_site_pool = original


class ContextSummaryLoggingTest(unittest.TestCase):
    def test_summary_call_is_logged_and_mirrored(self):
        from igngbot_v3 import context_manager as cm

        config = SimpleNamespace(
            CONTEXT_MAX_TOKENS=100,
            CONTEXT_SUMMARY_TRIGGER_RATIO=0.0,
            CONTEXT_SUMMARY_MAX_TOKENS=100,
            OPENAI_CHAT_MODEL="cloud-model",
            LLM_LOCAL_MODEL="local-model",
            CHAT_HISTORY_MAX_TOKENS=100,
            CHAT_HISTORY_MAX_MESSAGES=2,
        )
        db = SimpleNamespace(
            mark_context_summary_status=lambda *a: None,
            save_context_summary=lambda gid, text, boundary: {
                "summary_text": text,
                "summarized_through_id": boundary,
            },
        )

        class FakeLLM:
            async def chat_completion(self, **kwargs):
                return {
                    "choices": [{"message": {"content": "摘要结果"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }

        manager = cm.ContextManager(
            config,
            db,
            FakeLLM(),
            "sys-prompt",
            llm_options={"model": "local-model"},
        )
        rows = [
            {"msg_id": str(i), "sender_id": "u", "message_content": "内容" * i, "_db_id": i}
            for i in range(1, 6)
        ]

        logged = {}

        async def fake_insert(**kwargs):
            logged["call_log"] = kwargs
            return 77

        mirrored = {}

        async def fake_mirror(**kwargs):
            mirrored["site"] = kwargs

        original_insert = cm.insert_call_log
        original_mirror = cm.mirror_call_to_site
        cm.insert_call_log = fake_insert
        cm.mirror_call_to_site = fake_mirror
        try:
            summary, recent = asyncio.run(
                manager.compress_if_needed(
                    group_id=9,
                    rows=rows,
                    existing_summary=None,
                    system_prompt="sys",
                    user_prompt="user",
                )
            )
        finally:
            cm.insert_call_log = original_insert
            cm.mirror_call_to_site = original_mirror

        self.assertEqual(summary["summary_text"], "摘要结果")
        self.assertEqual(logged["call_log"]["call_type"], "summary")
        self.assertEqual(logged["call_log"]["model"], "local-model")
        self.assertEqual(logged["call_log"]["success"], True)
        self.assertEqual(mirrored["site"]["call_log_id"], 77)
        self.assertEqual(mirrored["site"]["call_type"], "summary")


if __name__ == "__main__":
    unittest.main()
