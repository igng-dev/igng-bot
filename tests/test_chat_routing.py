import unittest
from pathlib import Path
from types import SimpleNamespace

from igngbot_v3.chat_service import ChatService
from igngbot_v3.main import App


class ChatRoutingTest(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.config = SimpleNamespace(
            BOT_USER_ID=1000000001,
            CHAT_DIRECT_ALIASES=("云萤", "莹宝"),
        )
        self.app.db = SimpleNamespace(
            get_message_by_msg_id=lambda *_args: {"is_self": True, "sender_id": "1000000001"}
        )

    def test_group_messages_without_direct_address_are_skipped(self):
        for parsed in (
            {"conversation_type": "group", "message_content": "那周目服能水淹末地吗"},
            {"conversation_type": "group", "message_content": "@123456 你觉得呢", "message_structure": [{"type": "at", "qq": "123456"}]},
            {"conversation_type": "group", "message_content": "[图片]", "files": [{"type": "image"}]},
        ):
            self.assertTrue(self.app._should_skip_non_direct_chat_message(parsed, False, set()))

    def test_proactive_chat_mode_allows_plain_topic_messages(self):
        parsed = {
            "conversation_type": "group",
            "message_content": "那周目服能水淹末地吗",
            "message_structure": [],
        }
        self.assertFalse(self.app._should_skip_non_direct_chat_message(parsed, False, set(), True))

        addressed_to_other_user = {
            "conversation_type": "group",
            "message_content": "@123456 你觉得呢",
            "message_structure": [{"type": "at", "qq": "123456"}],
        }
        self.assertTrue(self.app._should_skip_non_direct_chat_message(
            addressed_to_other_user,
            False,
            {"123456"},
            True,
        ))

    def test_structured_and_textual_direct_addresses_are_allowed(self):
        structured = {
            "conversation_type": "group",
            "message_content": "@1000000001 你是哪个模型？",
            "message_structure": [{"type": "at", "qq": "1000000001"}],
        }
        self.assertTrue(self.app._is_direct_mention(structured))
        self.assertFalse(self.app._should_skip_non_direct_chat_message(structured, True, {"1000000001"}))

        for text in ("@云萤 你是哪个模型？", "莹宝，帮我看看这个", "云萤你觉得抽不抽"):
            parsed = {"conversation_type": "group", "message_content": text, "message_structure": []}
            self.assertTrue(self.app._has_textual_direct_alias(parsed), text)

    def test_name_mentioned_as_part_of_other_chat_is_not_direct(self):
        parsed = {
            "conversation_type": "group",
            "message_content": "你看莹宝也说抽哎",
            "message_structure": [],
        }
        self.assertFalse(self.app._has_textual_direct_alias(parsed))

    def test_private_messages_use_a_negative_history_key(self):
        from igngbot_v3.message_parser import parse_message
        parsed = parse_message({
            "message_type": "private",
            "user_id": 12345,
            "message_id": 77,
            "message": "你在吗",
        })
        self.assertEqual(parsed["group_id"], -12345)
        self.assertEqual(parsed["sender_id"], 12345)
        self.assertEqual(parsed["message_source"], "inbound")

    def test_legacy_mc_ticket_commands_are_disabled(self):
        self.assertTrue(App._is_disabled_mc_ticket_command("/服务器反馈 创建 服务器异常"))
        self.assertTrue(App._is_disabled_mc_ticket_command("/服务器反馈 待办"))
        self.assertFalse(App._is_disabled_mc_ticket_command("服务器反馈不是命令"))

    def test_prompt_contains_natural_persona_and_fact_guard(self):
        prompt = Path("prompts/system.txt").read_text(encoding="utf-8")
        self.assertIn("你是云萤，16岁，高一女生", prompt)
        self.assertIn("严禁一本正经地凭常识编造确定答案", prompt)
        self.assertIn("杜绝AI套话", prompt)
        self.assertIn("should_reply", prompt)

    def test_reply_cleanup_still_removes_internal_instructions(self):
        self.assertEqual(ChatService._normalize_chat_reply("请分析：具体内容"), "")


if __name__ == "__main__":
    unittest.main()
