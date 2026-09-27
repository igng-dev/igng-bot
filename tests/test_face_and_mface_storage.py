import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from igngbot_v3.message_parser import parse_message
from igngbot_v3.storage import StorageHandler


class FaceAndMFaceStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.nas_dir = os.path.join(self.tmp_dir, "nas_media")
        self.local_dir = os.path.join(self.tmp_dir, "local_runtime")
        os.makedirs(self.nas_dir, exist_ok=True)
        os.makedirs(self.local_dir, exist_ok=True)

        self.config = SimpleNamespace(
            MESSAGE_ROOT=os.path.join(self.nas_dir, "message_logs"),
            LEGACY_PATH_PREFIXES=("/mnt/media/message_logs",),
            STORAGE_REQUIRE_MOUNT=False,
            LOCAL_STORAGE=self.local_dir,
            MAX_FILE_SIZE=50 * 1024 * 1024,
        )
        self.storage = StorageHandler(self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_parse_message_extracts_mface_and_face(self):
        data = {
            "message_type": "group",
            "group_id": 123456,
            "user_id": 98765,
            "message_id": 4321,
            "message": [
                {"type": "text", "data": {"text": "看看这个"}},
                {"type": "face", "data": {"id": "277"}},
                {
                    "type": "mface",
                    "data": {
                        "emoji_id": "em_test_123",
                        "summary": "可爱猫猫",
                        "url": "https://example.com/em_test_123.gif",
                    },
                },
            ],
        }
        parsed = parse_message(data)
        self.assertIsNotNone(parsed)
        self.assertIn("[表情:277]", parsed["message_content"])
        self.assertIn("[贴纸: 可爱猫猫]", parsed["message_content"])

        files = parsed.get("files", [])
        types = [f["type"] for f in files]
        self.assertIn("face", types)
        self.assertIn("mface", types)

        face_item = next(f for f in files if f["type"] == "face")
        self.assertEqual(face_item["id"], "277")

        mface_item = next(f for f in files if f["type"] == "mface")
        self.assertEqual(mface_item["emoji_id"], "em_test_123")
        self.assertEqual(mface_item["summary"], "可爱猫猫")
        self.assertEqual(mface_item["url"], "https://example.com/em_test_123.gif")

    def test_mface_static_and_animated_compression(self):
        # 1. Create a dummy animated gif
        gif_path = os.path.join(self.tmp_dir, "sample.gif")
        frames = [
            Image.new("RGBA", (120, 120), (255, 0, 0, 255)),
            Image.new("RGBA", (120, 120), (0, 255, 0, 255)),
        ]
        frames[0].save(
            gif_path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=150,
            loop=0,
        )

        with open(gif_path, "rb") as f:
            gif_bytes = f.read()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": str(len(gif_bytes))}
        mock_resp.iter_content = lambda chunk_size=8192: [gif_bytes]
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            res = self.storage.download_and_store(
                file_url="https://example.com/sample.gif",
                group_id=123456,
                file_name="4321_mface_sample.gif",
                file_type="mface",
            )

        self.assertIsInstance(res, dict)
        out_path = res["stored_path"]
        thumb_path = res["thumb_path"]
        meta = res["meta"]

        self.assertIsNotNone(out_path)
        self.assertTrue(out_path.endswith(".webp"))
        self.assertTrue(os.path.isfile(out_path))

        self.assertIsNotNone(thumb_path)
        self.assertTrue(thumb_path.endswith("_thumb.webp"))
        self.assertTrue(os.path.isfile(thumb_path))

        self.assertTrue(meta["is_animated"])
        self.assertEqual(meta["width"], 120)
        self.assertEqual(meta["height"], 120)

        with Image.open(out_path) as out_img:
            self.assertEqual(out_img.format, "WEBP")
            self.assertTrue(getattr(out_img, "is_animated", False))
            self.assertEqual(getattr(out_img, "n_frames", 1), 2)

        # Thumbnail for animated mface is a static single-frame WebP
        with Image.open(thumb_path) as thumb_img:
            self.assertEqual(thumb_img.format, "WEBP")
            self.assertFalse(getattr(thumb_img, "is_animated", False))
            self.assertLessEqual(max(thumb_img.size), 360)

    def test_image_compression_and_thumbnail(self):
        # Create a large test image (2400 x 1600)
        img_path = os.path.join(self.tmp_dir, "large.jpg")
        large_img = Image.new("RGB", (2400, 1600), (100, 150, 200))
        large_img.save(img_path, format="JPEG", quality=95)

        with open(img_path, "rb") as f:
            jpg_bytes = f.read()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": str(len(jpg_bytes))}
        mock_resp.iter_content = lambda chunk_size=8192: [jpg_bytes]
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            res = self.storage.download_and_store(
                file_url="https://example.com/large.jpg",
                group_id=123456,
                file_name="5555_large.jpg",
                file_type="image",
            )

        self.assertIsInstance(res, dict)
        out_path = res["stored_path"]
        thumb_path = res["thumb_path"]
        meta = res["meta"]

        self.assertTrue(os.path.isfile(out_path))
        self.assertTrue(os.path.isfile(thumb_path))
        self.assertTrue(thumb_path.endswith("_thumb.webp"))

        # Main image dimension should be scaled down to max 1920
        self.assertEqual(meta["stored_width"], 1920)
        self.assertEqual(meta["stored_height"], 1280)
        # Thumb dimension should be scaled down to max 360
        self.assertEqual(meta["thumb_width"], 360)
        self.assertEqual(meta["thumb_height"], 240)

        with Image.open(thumb_path) as thumb_img:
            self.assertEqual(thumb_img.size, (360, 240))
            self.assertEqual(thumb_img.format, "WEBP")

    def test_store_face_if_missing_singleton_cache(self):
        # Create a dummy PNG for face
        img = Image.new("RGBA", (64, 64), (255, 255, 0, 255))
        face_buf_path = os.path.join(self.tmp_dir, "face_raw.png")
        img.save(face_buf_path, format="PNG")
        with open(face_buf_path, "rb") as f:
            png_bytes = f.read()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = png_bytes

        with patch("requests.get", return_value=mock_resp) as mock_get:
            res1 = self.storage.store_face_if_missing("277")
            self.assertIsInstance(res1, dict)
            path1 = res1["stored_path"]
            thumb1 = res1["thumb_path"]
            self.assertIsNotNone(path1)
            self.assertTrue(os.path.isfile(path1))
            self.assertTrue(path1.endswith("face_277.webp"))
            self.assertTrue(os.path.isfile(thumb1))
            self.assertTrue(thumb1.endswith("face_277_thumb.webp"))
            self.assertIn(os.path.join("assets", "faces"), path1)
            self.assertEqual(mock_get.call_count, 1)

            # Second call should use existing cached file and not call requests.get
            res2 = self.storage.store_face_if_missing("277")
            self.assertEqual(res1["stored_path"], res2["stored_path"])
            self.assertEqual(res1["thumb_path"], res2["thumb_path"])
            self.assertEqual(mock_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
