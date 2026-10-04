import os
import subprocess
import shutil
import logging
import threading
from datetime import datetime

import requests
from PIL import Image, ImageSequence

logger = logging.getLogger(__name__)

# WebP quality (0-100, lower = smaller)
IMAGE_WEBP_QUALITY = 80
IMAGE_MAX_DIMENSION = 1920

# MFace (sticker/custom emoji) WebP parameters
MFACE_WEBP_QUALITY = 80
MFACE_MAX_DIMENSION = 1080

# WebP low-resolution thumbnail parameters for web/chat preview
THUMB_WEBP_QUALITY = 30
THUMB_MAX_DIMENSION = 360

# H.265 encoding params
VIDEO_CRF = 28
VIDEO_MAX_WIDTH = 1280
VIDEO_MAX_HEIGHT = 720
VIDEO_AUDIO_BITRATE = "64k"
VIDEO_ENCODER = "libx265"

# QQ Face (system yellow face) public CDN URL templates
QQ_FACE_CDN_TEMPLATES = (
    "https://qzonestyle.gtimg.cn/qzone/em/e{face_id}.gif",
    "https://qzonestyle.gtimg.cn/qzone/em/e{face_id}.png",
)


class StorageHandler:
    def __init__(self, config):
        self.config = config
        self._available = False
        self._nas_available = False
        self._checked = False
        self._storage_root = self._configured_root(config)
        self._face_lock = threading.Lock()

    @staticmethod
    def _configured_root(config):
        """Attachment root, accepting both the new and the legacy attribute name."""
        root = getattr(config, "MESSAGE_ROOT", None) or getattr(config, "NAS_MOUNT_PATH", None)
        if not root:
            raise RuntimeError(
                "Attachment storage root is not configured "
                "(set MESSAGE_ROOT, or the legacy NAS_MOUNT_PATH)"
            )
        return root

    def _legacy_prefixes(self) -> tuple[str, ...]:
        prefixes = getattr(self.config, "LEGACY_PATH_PREFIXES", ()) or ()
        return tuple(str(item).rstrip("/") for item in prefixes if str(item).strip())

    def check_available(self):
        """Validate the attachment root.

        The container bind-mounts the NAS data directory, so the old CIFS
        semantics (``os.path.ismount``) no longer describe the deployment and a
        missing directory must be a loud failure: silently falling back to local
        storage would keep the bot "working" while every attachment stayed
        invisible to the site. Called once at startup; after that the resolved
        root is reused so a transient probe failure cannot move media mid-run.
        """
        if self._checked:
            return self._available
        self._checked = True

        root = self._configured_root(self.config)
        usable = os.path.isdir(root) and os.access(root, os.W_OK)
        if usable:
            # Confirm the root really sits on a mount instead of an empty
            # container-local directory created by a forgotten bind mount.
            on_mount = os.path.ismount(root) or os.path.ismount(os.path.dirname(root) or "/")
            self._nas_available = on_mount
            self._available = True
            self._storage_root = root
            logger.info(
                "Attachment storage ready: %s (mount=%s)", self._storage_root, on_mount
            )
            if not on_mount:
                logger.warning(
                    "Attachment root %s is writable but is not a mount point; "
                    "verify the compose bind mount is configured",
                    root,
                )
            return True

        require_mount = bool(getattr(self.config, "STORAGE_REQUIRE_MOUNT", True))
        detail = f"Attachment root {root} is missing or not writable"
        if require_mount:
            self._available = False
            self._nas_available = False
            self._storage_root = root
            logger.error("%s; refusing to start with a local fallback", detail)
            raise RuntimeError(detail)

        fallback = os.path.join(self.config.LOCAL_STORAGE, "attachments")
        os.makedirs(fallback, exist_ok=True)
        self._storage_root = fallback
        self._available = True
        self._nas_available = False
        logger.warning("%s; falling back to local storage: %s", detail, fallback)
        return True

    def resolve_path(self, stored_path):
        """Map a stored path onto the current attachment root.

        Rows written before the container migration carry host paths such as
        ``/mnt/media/message_logs/<group>/<date>/<file>``. The same tree is now
        bind-mounted at the container root, so strip any known legacy prefix and
        re-anchor the remainder. Paths that already exist are returned untouched.
        """
        raw = str(stored_path or "").strip()
        if not raw:
            return ""
        if os.path.exists(raw):
            return raw

        normalised = raw.replace("\\", "/")
        for prefix in self._legacy_prefixes():
            if normalised == prefix:
                candidate = self._storage_root
            elif normalised.startswith(prefix + "/"):
                candidate = os.path.join(self._storage_root, normalised[len(prefix) + 1:])
            else:
                continue
            if os.path.exists(candidate):
                return candidate
            # Keep the re-anchored path even when the file is absent: callers
            # decide what a missing attachment means.
            return candidate

        if os.path.isabs(normalised):
            return normalised
        candidate = os.path.join(self._storage_root, normalised)
        return candidate if os.path.exists(candidate) else normalised

    def to_stored_path(self, absolute_path):
        """Render an absolute path the way it should be persisted.

        The canonical form is the in-container absolute path
        (``<MESSAGE_ROOT>/<group>/<date>/<file>``). Keeping the final
        ``message_logs`` segment is deliberate: the IGNG site derives the media
        URL by slicing on that marker, so the persisted value must always carry
        it regardless of what the host-side directory is called.
        """
        raw = str(absolute_path or "").strip()
        if not raw:
            return ""
        normalised = raw.replace("\\", "/")
        root = self._storage_root.replace("\\", "/").rstrip("/")
        if normalised == root or normalised.startswith(root + "/"):
            return normalised

        for prefix in self._legacy_prefixes():
            if normalised == prefix:
                return root
            if normalised.startswith(prefix + "/"):
                return f"{root}/{normalised[len(prefix) + 1:]}"

        if os.path.isabs(normalised):
            return normalised
        return f"{root}/{normalised}"


    def _storage_path(self, group_id, file_name):
        file_name = os.path.basename(str(file_name).replace("\\", "/"))
        if not file_name or file_name in {".", ".."} or "\0" in file_name:
            raise ValueError("invalid attachment filename")
        if not str(group_id).lstrip("-").isdigit():
            raise ValueError("invalid attachment conversation")
        date_str = datetime.now().strftime("%Y-%m-%d")
        dir_path = os.path.join(
            self._storage_root, str(group_id), date_str
        )
        os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, file_name)

    def _faces_storage_dir(self):
        faces_dir = os.path.join(self._storage_root, "assets", "faces")
        os.makedirs(faces_dir, exist_ok=True)
        return faces_dir

    def _process_image(
        self,
        input_path,
        output_path,
        thumb_path=None,
        max_dimension=IMAGE_MAX_DIMENSION,
        quality=IMAGE_WEBP_QUALITY,
        thumb_max_dimension=THUMB_MAX_DIMENSION,
        thumb_quality=THUMB_WEBP_QUALITY,
    ):
        """Compress image to WebP and optionally generate a lightweight low-res WebP thumbnail."""
        with Image.open(input_path) as img:
            orig_w, orig_h = img.size
            is_animated = bool(getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1)

            meta = {
                "width": orig_w,
                "height": orig_h,
                "is_animated": is_animated,
            }

            if is_animated:
                frames = []
                durations = []
                main_w, main_h = orig_w, orig_h
                if max(orig_w, orig_h) > max_dimension:
                    ratio = max_dimension / max(orig_w, orig_h)
                    main_w, main_h = int(orig_w * ratio), int(orig_h * ratio)

                for frame in ImageSequence.Iterator(img):
                    f = frame.copy()
                    if (main_w, main_h) != (orig_w, orig_h):
                        f = f.resize((main_w, main_h), Image.LANCZOS)
                    frames.append(f)
                    durations.append(frame.info.get("duration", 100))

                frames[0].save(
                    output_path,
                    "WEBP",
                    save_all=True,
                    append_images=frames[1:],
                    quality=quality,
                    duration=durations,
                    loop=img.info.get("loop", 0),
                    optimize=True,
                )
                meta["stored_width"] = main_w
                meta["stored_height"] = main_h

                if thumb_path:
                    # For animated stickers/GIFs, extract first frame as a tiny static thumbnail (few KB)
                    first_frame = frames[0].copy().convert("RGBA")
                    t_ratio = min(1.0, thumb_max_dimension / max(first_frame.width, first_frame.height))
                    t_w, t_h = int(first_frame.width * t_ratio), int(first_frame.height * t_ratio)
                    if (t_w, t_h) != (first_frame.width, first_frame.height):
                        first_frame = first_frame.resize((t_w, t_h), Image.LANCZOS)
                    first_frame.save(thumb_path, "WEBP", quality=thumb_quality, optimize=True)
                    meta["thumb_width"] = t_w
                    meta["thumb_height"] = t_h
            else:
                main_w, main_h = orig_w, orig_h
                if max(orig_w, orig_h) > max_dimension:
                    ratio = max_dimension / max(orig_w, orig_h)
                    main_w, main_h = int(orig_w * ratio), int(orig_h * ratio)
                    save_img = img.resize((main_w, main_h), Image.LANCZOS)
                else:
                    save_img = img

                save_img.save(output_path, "WEBP", quality=quality, optimize=True)
                meta["stored_width"] = main_w
                meta["stored_height"] = main_h

                if thumb_path:
                    t_ratio = min(1.0, thumb_max_dimension / max(orig_w, orig_h))
                    t_w, t_h = int(orig_w * t_ratio), int(orig_h * t_ratio)
                    thumb_img = save_img.resize((t_w, t_h), Image.LANCZOS) if (t_w, t_h) != (main_w, main_h) else save_img
                    thumb_img.save(thumb_path, "WEBP", quality=thumb_quality, optimize=True)
                    meta["thumb_width"] = t_w
                    meta["thumb_height"] = t_h

        original_size = os.path.getsize(input_path)
        compressed_size = os.path.getsize(output_path)
        saved = (1 - compressed_size / original_size) * 100 if original_size else 0
        thumb_info = ""
        if thumb_path and os.path.exists(thumb_path):
            thumb_size = os.path.getsize(thumb_path)
            thumb_info = f", thumb: {thumb_size / 1024:.1f}K"

        logger.info(
            f"Image ({'animated' if is_animated else 'static'}): {os.path.basename(input_path)} -> WebP, "
            f"{original_size / 1024:.0f}K -> {compressed_size / 1024:.0f}K "
            f"({saved:.0f}% saved{thumb_info})"
        )
        return meta

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
            "ffmpeg", "-y", "-i", input_path,
            "-vf", scale_filter,
            "-c:v", VIDEO_ENCODER, "-crf", str(VIDEO_CRF),
            "-preset", "medium",
        ]
        if has_audio:
            cmd.extend(["-c:a", "aac", "-b:a", VIDEO_AUDIO_BITRATE])
        else:
            cmd.append("-an")
        cmd.append(output_path)

        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
        original_size = os.path.getsize(input_path)
        compressed_size = os.path.getsize(output_path)
        saved = (1 - compressed_size / original_size) * 100 if original_size else 0
        logger.info(
            f"Video: {os.path.basename(input_path)} -> H.265, "
            f"{original_size / 1024:.0f}K -> {compressed_size / 1024:.0f}K "
            f"({saved:.0f}% saved)"
        )

    def download_and_store(self, file_url, group_id, file_name, file_type):
        if not self._checked:
            self.check_available()

        local_path = None
        try:
            logger.info("Downloading attachment type=%s", file_type)
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
            written = 0
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        written += len(chunk)
                        if written > self.config.MAX_FILE_SIZE:
                            raise ValueError("attachment exceeds configured byte limit")
                        f.write(chunk)

            if file_type == "image":
                stem = os.path.splitext(file_name)[0]
                out_path = self._storage_path(group_id, stem + ".webp")
                thumb_path = self._storage_path(group_id, stem + "_thumb.webp")
                meta = self._process_image(
                    local_path,
                    out_path,
                    thumb_path=thumb_path,
                    max_dimension=IMAGE_MAX_DIMENSION,
                    quality=IMAGE_WEBP_QUALITY,
                )
                return {
                    "stored_path": out_path,
                    "thumb_path": thumb_path,
                    "meta": meta,
                }
            elif file_type == "mface":
                stem = os.path.splitext(file_name)[0]
                out_path = self._storage_path(group_id, stem + ".webp")
                thumb_path = self._storage_path(group_id, stem + "_thumb.webp")
                meta = self._process_image(
                    local_path,
                    out_path,
                    thumb_path=thumb_path,
                    max_dimension=MFACE_MAX_DIMENSION,
                    quality=MFACE_WEBP_QUALITY,
                )
                return {
                    "stored_path": out_path,
                    "thumb_path": thumb_path,
                    "meta": meta,
                }
            elif file_type == "video":
                out_name = os.path.splitext(file_name)[0] + ".mp4"
                out_path = self._storage_path(group_id, out_name)
                self._process_video(local_path, out_path)
                return {
                    "stored_path": out_path,
                    "thumb_path": None,
                    "meta": None,
                }
            else:
                out_path = self._storage_path(group_id, file_name)
                shutil.copy2(local_path, out_path)
                logger.info(f"File copied: {out_path}")
                return {
                    "stored_path": out_path,
                    "thumb_path": None,
                    "meta": None,
                }

        except subprocess.TimeoutExpired:
            logger.error("Video encoding timed out")
            return None
        except Exception as e:
            logger.error("Failed to process/store attachment type=%s error=%s", file_type, type(e).__name__)
            return None
        finally:
            if local_path and os.path.exists(local_path):
                os.unlink(local_path)

    def store_face_if_missing(self, face_id: str | int, direct_url: str = "") -> dict | None:
        """Fetch and cache a standard QQ face into the shared assets library if not already cached."""
        face_id_str = str(face_id or "").strip()
        if not face_id_str:
            return None

        if not self._checked:
            self.check_available()

        faces_dir = self._faces_storage_dir()
        target_path = os.path.join(faces_dir, f"face_{face_id_str}.webp")
        thumb_path = os.path.join(faces_dir, f"face_{face_id_str}_thumb.webp")

        if os.path.isfile(target_path) and os.path.getsize(target_path) > 0:
            return {
                "stored_path": target_path,
                "thumb_path": thumb_path if os.path.isfile(thumb_path) else None,
                "meta": None,
            }

        with self._face_lock:
            if os.path.isfile(target_path) and os.path.getsize(target_path) > 0:
                return {
                    "stored_path": target_path,
                    "thumb_path": thumb_path if os.path.isfile(thumb_path) else None,
                    "meta": None,
                }

            tmp_dir = f"{self.config.LOCAL_STORAGE}/tmp"
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_download = os.path.join(tmp_dir, f"face_dl_{face_id_str}_{datetime.now().timestamp()}.tmp")

            download_urls = []
            if direct_url:
                download_urls.append(direct_url)
            for tmpl in QQ_FACE_CDN_TEMPLATES:
                download_urls.append(tmpl.format(face_id=face_id_str))

            downloaded = False
            for url in download_urls:
                try:
                    resp = requests.get(url, timeout=10)
                    if resp.status_code == 200 and resp.content:
                        with open(tmp_download, "wb") as f:
                            f.write(resp.content)
                        downloaded = True
                        break
                except Exception as exc:
                    logger.debug("Failed to download QQ face %s from %s: %s", face_id_str, url, exc)

            if not downloaded:
                logger.warning("QQ face %s could not be fetched from known CDN endpoints", face_id_str)
                return None

            try:
                meta = self._process_image(
                    tmp_download,
                    target_path,
                    thumb_path=thumb_path,
                    max_dimension=MFACE_MAX_DIMENSION,
                    quality=MFACE_WEBP_QUALITY,
                )
                logger.info("Cached QQ face %s to %s", face_id_str, target_path)
                return {
                    "stored_path": target_path,
                    "thumb_path": thumb_path,
                    "meta": meta,
                }
            except Exception as exc:
                logger.warning("Failed to process face %s: %s", face_id_str, exc)
                return None
            finally:
                if os.path.exists(tmp_download):
                    os.unlink(tmp_download)
