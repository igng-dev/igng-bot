import unittest
import os
import tempfile
import json
from types import SimpleNamespace
from PIL import Image

from igngbot_v3.chat_service import ChatService, MessageContext


class ChatMultimodalTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.img1_path = os.path.join(self.tmp_dir.name, "test1.png")
        self.img2_path = os.path.join(self.tmp_dir.name, "test2.png")
        
        # Create small test images
        img = Image.new("RGB", (50, 50), color="red")
        img.save(self.img1_path)
        img = Image.new("RGB", (60, 60), color="blue")
        img.save(self.img2_path)

        self.config_multimodal_on = SimpleNamespace(
            LLM_LOCAL_MULTIMODAL=True,
            PROMPT_DIR="prompts",
            OPENAI_BASE_URL="http://dummy",
            OPENAI_API_KEY="dummy",
            OPENAI_CHAT_MODEL="dummy",
            CONTEXT_SUMMARY_PROMPT="",
            CONTEXT_MAX_TOKENS=8192,
            CONTEXT_SUMMARY_TRIGGER_RATIO=0.82,
        )
        self.config_multimodal_off = SimpleNamespace(
            LLM_LOCAL_MULTIMODAL=False,
            PROMPT_DIR="prompts",
            OPENAI_BASE_URL="http://dummy",
            OPENAI_API_KEY="dummy",
            OPENAI_CHAT_MODEL="dummy",
            CONTEXT_SUMMARY_PROMPT="",
            CONTEXT_MAX_TOKENS=8192,
            CONTEXT_SUMMARY_TRIGGER_RATIO=0.82,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_multimodal_disabled_returns_string(self):
        svc = ChatService.__new__(ChatService)
        svc.config = self.config_multimodal_off
        
        ctx = MessageContext(
            group_id=123,
            sender_id=456,
            sender_name="tester",
            msg_id="1001",
            rich_text="看图",
            direct_mention=True,
            files=[{"type": "image", "stored_path": self.img1_path}],
        )
        content = svc._build_user_content(ctx, "prompt text", [])
        self.assertIsInstance(content, str)
        self.assertEqual(content, "prompt text")

    def test_multimodal_enabled_attaches_current_and_history_images(self):
        svc = ChatService.__new__(ChatService)
        svc.config = self.config_multimodal_on
        
        ctx = MessageContext(
            group_id=123,
            sender_id=456,
            sender_name="tester",
            msg_id="1001",
            rich_text="看图",
            direct_mention=True,
            files=[{"type": "image", "stored_path": self.img1_path}],
        )
        history = [
            {
                "msg_id": "1000",
                "sender_id": 789,
                "attachments_json": json.dumps([{"type": "image", "stored_path": self.img2_path}]),
            }
        ]
        content = svc._build_user_content(ctx, "prompt text", history)
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 3)  # text + 2 images
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[0]["text"], "prompt text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/webp;base64,"))
        self.assertEqual(content[2]["type"], "image_url")
        self.assertTrue(content[2]["image_url"]["url"].startswith("data:image/webp;base64,"))

    def test_missing_image_file_falls_back_gracefully(self):
        svc = ChatService.__new__(ChatService)
        svc.config = self.config_multimodal_on
        
        ctx = MessageContext(
            group_id=123,
            sender_id=456,
            sender_name="tester",
            msg_id="1001",
            rich_text="看图",
            direct_mention=True,
            files=[{"type": "image", "stored_path": "/nonexistent/path.png"}],
        )
        content = svc._build_user_content(ctx, "prompt text", [])
        self.assertIsInstance(content, str)
        self.assertEqual(content, "prompt text")


if __name__ == "__main__":
    unittest.main()
