import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from igngbot_v3.media_text import MediaTextExtractor, MediaTextResult, append_media_text


class MediaConfig:
    MEDIA_OCR_ENABLED = True
    MEDIA_OCR_PROVIDER = "rapidocr"
    MEDIA_OCR_MIN_SCORE = 0.25
    MEDIA_OCR_MAX_CHARS = 4000
    MEDIA_ASR_ENABLED = True
    MEDIA_ASR_PROVIDER = "faster-whisper"
    MEDIA_ASR_MODEL = "small"
    MEDIA_ASR_MODEL_DIR = ""
    MEDIA_ASR_DEVICE = "cpu"
    MEDIA_ASR_COMPUTE_TYPE = "int8"
    MEDIA_ASR_LANGUAGE = "zh"
    MEDIA_ASR_BEAM_SIZE = 5
    MEDIA_ASR_VAD_FILTER = True
    MEDIA_ASR_MAX_CHARS = 6000


class MediaTextTest(unittest.TestCase):
    def test_rapidocr_result_is_normalized_and_low_scores_are_ignored(self):
        fake_module = types.ModuleType("rapidocr_onnxruntime")

        class FakeRapidOCR:
            def __call__(self, _path):
                return (
                    [
                        ([[0, 0]], "  菜单  ", 0.99),
                        ([[0, 0]], "噪声", 0.1),
                        ([[0, 0]], "价格", 0.8),
                    ],
                    0.01,
                )

        fake_module.RapidOCR = FakeRapidOCR
        with tempfile.NamedTemporaryFile(suffix=".webp") as image_file:
            with patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_module}):
                result = MediaTextExtractor(MediaConfig()).extract("image", image_file.name)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.backend, "rapidocr")
        self.assertEqual(result.text, "菜单\n价格")

    def test_faster_whisper_segments_are_joined(self):
        fake_module = types.ModuleType("faster_whisper")

        class Segment:
            def __init__(self, text):
                self.text = text

        class FakeWhisperModel:
            def __init__(self, *_args, **_kwargs):
                pass

            def transcribe(self, _path, **_kwargs):
                return iter([Segment("你好"), Segment(" 世界 ")]), object()

        fake_module.WhisperModel = FakeWhisperModel
        with tempfile.NamedTemporaryFile(suffix=".ogg") as audio_file:
            with patch.dict(sys.modules, {"faster_whisper": fake_module}):
                result = MediaTextExtractor(MediaConfig()).extract("record", audio_file.name)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.backend, "faster-whisper")
        self.assertEqual(result.text, "你好 世界")

    def test_media_text_is_added_to_the_shared_llm_message(self):
        parsed = {
            "message_content": "[图片][语音]",
            "plain_text_content": "",
            "audio_transcript": "",
        }
        image = {"type": "image"}
        audio = {"type": "record"}
        append_media_text(
            parsed,
            [
                (image, MediaTextResult("服务器状态", "success", "rapidocr")),
                (audio, MediaTextResult("现在正常", "success", "faster-whisper")),
            ],
        )
        self.assertNotIn("[图片OCR]", parsed["message_content"])
        self.assertNotIn("服务器状态", parsed["message_content"])
        self.assertIn("[语音转写]", parsed["message_content"])
        self.assertIn("现在正常", parsed["plain_text_content"])
        self.assertEqual(parsed["audio_transcript"], "现在正常")
        self.assertEqual(image["ocr_text"], "服务器状态")
        self.assertEqual(audio["transcript"], "现在正常")


if __name__ == "__main__":
    unittest.main()
