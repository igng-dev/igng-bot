import os
import subprocess
import shutil
import logging
from datetime import datetime

import requests
from PIL import Image

logger = logging.getLogger(__name__)

# WebP quality (0-100, lower = smaller)
IMAGE_WEBP_QUALITY = 80
IMAGE_MAX_DIMENSION = 1920

# H.265 encoding params
VIDEO_CRF = 28
VIDEO_MAX_WIDTH = 1280
VIDEO_MAX_HEIGHT = 720
VIDEO_AUDIO_BITRATE = "64k"
VIDEO_ENCODER = "libx265"


class StorageHandler:
    def __init__(self, config):
        self.config = config
        self._available = False
        self._nas_available = False
        self._storage_root = self.config.NAS_MOUNT_PATH

    def check_available(self):
        self._nas_available = os.path.ismount(self.config.NAS_MOUNT_BASE)
        if not self._nas_available:
            self._nas_available = os.path.exists(self.config.NAS_MOUNT_PATH)
        if self._nas_available:
            self._available = True
            self._storage_root = self.config.NAS_MOUNT_PATH
            logger.info(
                f"NAS storage ready: {self._storage_root}"
            )
        else:
            self._available = True
            self._storage_root = os.path.join(self.config.LOCAL_STORAGE, "attachments")
            os.makedirs(self._storage_root, exist_ok=True)
            self._available = True
            logger.warning(
                f"NAS mount not found at {self.config.NAS_MOUNT_BASE}, falling back to local storage: {self._storage_root}"
            )
        return True

    def _storage_path(self, group_id, file_name):
        date_str = datetime.now().strftime("%Y-%m-%d")
        dir_path = os.path.join(
            self._storage_root, str(group_id), date_str
        )
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, file_name)

    def _process_image(self, input_path, output_path):
        img = Image.open(input_path)

        # Limit max dimension while preserving aspect ratio
        if max(img.width, img.height) > IMAGE_MAX_DIMENSION:
            ratio = IMAGE_MAX_DIMENSION / max(img.width, img.height)
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        img.save(output_path, "WEBP", quality=IMAGE_WEBP_QUALITY, optimize=True)
        original_size = os.path.getsize(input_path)
        compressed_size = os.path.getsize(output_path)
        saved = (1 - compressed_size / original_size) * 100 if original_size else 0
        logger.info(
            f"Image: {os.path.basename(input_path)} -> WebP, "
            f"{original_size / 1024:.0f}K -> {compressed_size / 1024:.0f}K "
            f"({saved:.0f}% saved)"
        )

    def _process_video(self, input_path, output_path):
        # Check if video has an audio stream
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", input_path],
            capture_output=True, text=True, timeout=15,
        )
        has_audio = "audio" in probe.stdout.strip()

        scale_filter = (
            f"scale='min({VIDEO_MAX_WIDTH},iw)':'min({VIDEO_MAX_HEIGHT},ih)':"
            f"force_original_aspect_ratio=decrease"
        )
        cmd = [
            "ffmpeg", "-i", input_path,
            "-c:v", VIDEO_ENCODER,
            "-crf", str(VIDEO_CRF),
            "-vf", scale_filter,
        ]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", "-y", output_path]

        logger.info(f"Encoding video ({'with' if has_audio else 'no'} audio): {os.path.basename(input_path)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")
        original_size = os.path.getsize(input_path)
        compressed_size = os.path.getsize(output_path)
        saved = (1 - compressed_size / original_size) * 100 if original_size else 0
        logger.info(
            f"Video: {os.path.basename(input_path)} -> H.265, "
            f"{original_size / 1024:.0f}K -> {compressed_size / 1024:.0f}K "
            f"({saved:.0f}% saved)"
        )

    def download_and_store(self, file_url, group_id, file_name, file_type):
        if not self._available or not self._nas_available:
            self.check_available()

        local_path = None
        try:
            logger.info(f"Downloading {file_type}: {file_url[:80]}...")
            resp = requests.get(file_url, timeout=30, stream=True)
            resp.raise_for_status()

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > self.config.MAX_FILE_SIZE:
                logger.warning(
                    f"File too large ({content_length / 1024 / 1024:.0f}MB > 50MB), skipping"
                )
                return None

            tmp_dir = f"{self.config.LOCAL_STORAGE}/tmp"
            os.makedirs(tmp_dir, exist_ok=True)

            ext = os.path.splitext(file_name)[1] or ".tmp"
            local_path = os.path.join(tmp_dir, f"dl_{datetime.now().timestamp()}{ext}")
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            if file_type == "image":
                out_name = os.path.splitext(file_name)[0] + ".webp"
                out_path = self._storage_path(group_id, out_name)
                self._process_image(local_path, out_path)
            elif file_type == "video":
                out_name = os.path.splitext(file_name)[0] + ".mp4"
                out_path = self._storage_path(group_id, out_name)
                self._process_video(local_path, out_path)
            else:
                out_path = self._storage_path(group_id, file_name)
                shutil.copy2(local_path, out_path)
                logger.info(f"File copied: {out_path}")

            return out_path

        except subprocess.TimeoutExpired:
            logger.error(f"Video encoding timed out for {file_url}")
            return None
        except Exception as e:
            logger.error(f"Failed to process/store {file_type} {file_url}: {e}")
            return None
        finally:
            if local_path and os.path.exists(local_path):
                os.unlink(local_path)
