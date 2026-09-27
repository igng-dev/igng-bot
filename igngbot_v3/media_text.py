"""Local OCR and speech-to-text for incoming OneBot media.

The media models are loaded lazily so a missing optional model dependency does
not prevent the bot from receiving ordinary messages.  A remote OpenAI-
compatible backend can be selected when the deployment does not want to host
the OCR/ASR models locally.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import subprocess
import threading
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaTextResult:
    text: str = ""
    status: str = "empty"
    backend: str = ""
    error: str = ""


class MediaTextExtractor:
    """Extract visible text and spoken text from stored message attachments."""

    def __init__(self, config):
        self.config = config
        self._ocr_engine = None
        self._asr_model = None
        self._ocr_init_lock = threading.Lock()
        self._asr_init_lock = threading.Lock()
        self._ocr_call_lock = threading.Lock()
        self._asr_call_lock = threading.Lock()

    def extract(self, media_type: str, file_path: str) -> MediaTextResult:
        if not file_path or not os.path.isfile(file_path):
            return MediaTextResult(status="unavailable", error="media file does not exist")
        if media_type == "image":
            return self.extract_image_text(file_path)
        if media_type in ("audio", "record"):
            return self.extract_audio_text(file_path)
        return MediaTextResult(status="unsupported", error=f"unsupported media type: {media_type}")

    def extract_image_text(self, file_path: str) -> MediaTextResult:
        if not getattr(self.config, "MEDIA_OCR_ENABLED", True):
            return MediaTextResult(status="disabled", backend="disabled")

        provider = self._provider("MEDIA_OCR_PROVIDER", "rapidocr")
        if provider in ("off", "none", "disabled"):
            return MediaTextResult(status="disabled", backend=provider)

        local_error = None
        if provider in ("rapidocr", "local", "auto"):
            try:
                text = self._rapidocr(file_path)
                return MediaTextResult(
                    text=text,
                    status="success" if text else "no_text",
                    backend="rapidocr",
                )
            except Exception as exc:
                local_error = exc
                logger.warning("Local OCR failed for %s: %s", file_path, exc)
                if provider != "auto":
                    return MediaTextResult(
                        status="unavailable",
                        backend="rapidocr",
                        error=str(exc),
                    )

        if provider in ("vision_llm", "remote", "auto"):
            try:
                text = self._vision_llm_ocr(file_path)
                return MediaTextResult(
                    text=text,
                    status="success" if text else "no_text",
                    backend="vision_llm",
                )
            except Exception as exc:
                logger.warning("Vision OCR failed for %s: %s", file_path, exc)
                return MediaTextResult(
                    status="unavailable",
                    backend="vision_llm",
                    error=str(exc),
                )

        return MediaTextResult(
            status="unavailable",
            backend=provider,
            error=str(local_error or "no OCR backend configured"),
        )

    def extract_audio_text(self, file_path: str) -> MediaTextResult:
        if not getattr(self.config, "MEDIA_ASR_ENABLED", True):
            return MediaTextResult(status="disabled", backend="disabled")

        provider = self._provider("MEDIA_ASR_PROVIDER", "faster-whisper")
        if provider in ("off", "none", "disabled"):
            return MediaTextResult(status="disabled", backend=provider)

        local_error = None
        if provider in ("faster-whisper", "whisper", "local", "auto"):
            try:
                text = self._faster_whisper(file_path)
                return MediaTextResult(
                    text=text,
                    status="success" if text else "no_speech",
                    backend="faster-whisper",
                )
            except Exception as exc:
                local_error = exc
                logger.warning("Local ASR failed for %s: %s", file_path, exc)
                if provider != "auto":
                    return MediaTextResult(
                        status="unavailable",
                        backend="faster-whisper",
                        error=str(exc),
                    )

        if provider in ("remote", "openai", "auto"):
            try:
                text = self._remote_asr(file_path)
                return MediaTextResult(
                    text=text,
                    status="success" if text else "no_speech",
                    backend="remote-asr",
                )
            except Exception as exc:
                logger.warning("Remote ASR failed for %s: %s", file_path, exc)
                return MediaTextResult(
                    status="unavailable",
                    backend="remote-asr",
                    error=str(exc),
                )

        return MediaTextResult(
            status="unavailable",
            backend=provider,
            error=str(local_error or "no ASR backend configured"),
        )

    def _rapidocr(self, file_path: str) -> str:
        with self._ocr_init_lock:
            if self._ocr_engine is None:
                from rapidocr_onnxruntime import RapidOCR

                self._ocr_engine = RapidOCR()
        with self._ocr_call_lock:
            result, _ = self._ocr_engine(file_path)

        texts = []
        minimum_score = float(getattr(self.config, "MEDIA_OCR_MIN_SCORE", 0.25))
        for item in result or []:
            if isinstance(item, dict):
                value = item.get("text") or item.get("txt") or ""
                score = item.get("score", item.get("confidence", 1.0))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                value = item[1]
                score = item[2] if len(item) >= 3 else 1.0
            else:
                continue
            try:
                if float(score) < minimum_score:
                    continue
            except (TypeError, ValueError):
                pass
            value = str(value or "").strip()
            if value:
                texts.append(value)
        return self._normalize_text("\n".join(texts), "MEDIA_OCR_MAX_CHARS")

    def _faster_whisper(self, file_path: str) -> str:
        with self._asr_init_lock:
            if self._asr_model is None:
                from faster_whisper import WhisperModel

                model_name = str(getattr(self.config, "MEDIA_ASR_MODEL", "small"))
                model_dir = str(getattr(self.config, "MEDIA_ASR_MODEL_DIR", "") or "").strip()
                kwargs = {
                    "device": str(getattr(self.config, "MEDIA_ASR_DEVICE", "cpu")),
                    "compute_type": str(getattr(self.config, "MEDIA_ASR_COMPUTE_TYPE", "int8")),
                }
                if model_dir:
                    os.makedirs(model_dir, exist_ok=True)
                    kwargs["download_root"] = model_dir
                self._asr_model = WhisperModel(model_name, **kwargs)

        with self._asr_call_lock, self._prepared_audio(file_path) as audio_path:
            segments, _ = self._asr_model.transcribe(
                audio_path,
                language=str(getattr(self.config, "MEDIA_ASR_LANGUAGE", "zh") or "zh"),
                beam_size=int(getattr(self.config, "MEDIA_ASR_BEAM_SIZE", 5)),
                vad_filter=bool(getattr(self.config, "MEDIA_ASR_VAD_FILTER", True)),
            )
            text = " ".join(
                str(getattr(segment, "text", "") or "").strip()
                for segment in segments
                if str(getattr(segment, "text", "") or "").strip()
            )
        return self._normalize_text(text, "MEDIA_ASR_MAX_CHARS")

    def _vision_llm_ocr(self, file_path: str) -> str:
        base_url = str(getattr(self.config, "MEDIA_OCR_BASE_URL", "") or "").strip()
        if not base_url:
            raise RuntimeError("MEDIA_OCR_BASE_URL is not configured")
        model = str(getattr(self.config, "MEDIA_OCR_MODEL", "") or "").strip()
        if not model:
            raise RuntimeError("MEDIA_OCR_MODEL is not configured")

        with open(file_path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode("ascii")
        mime = mimetypes.guess_type(file_path)[0] or "image/png"
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "请只提取图片中实际可见的文字，保持原有语言和大致换行。"
                                "不要描述画面，不要猜测；如果没有文字，只输出空字符串。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": int(getattr(self.config, "MEDIA_OCR_MAX_TOKENS", 1024)),
        }
        data = self._post_json(
            self._endpoint(base_url, "/chat/completions"),
            payload,
            getattr(self.config, "MEDIA_OCR_API_KEY", ""),
            float(getattr(self.config, "MEDIA_OCR_TIMEOUT", 90)),
        )
        return self._normalize_text(self._response_text(data), "MEDIA_OCR_MAX_CHARS")

    def _remote_asr(self, file_path: str) -> str:
        base_url = str(getattr(self.config, "MEDIA_ASR_BASE_URL", "") or "").strip()
        if not base_url:
            raise RuntimeError("MEDIA_ASR_BASE_URL is not configured")
        model = str(getattr(self.config, "MEDIA_ASR_MODEL", "") or "").strip()
        if not model:
            raise RuntimeError("MEDIA_ASR_MODEL is not configured")

        import requests

        endpoint = self._endpoint(base_url, "/audio/transcriptions")
        headers = self._auth_headers(
            getattr(self.config, "MEDIA_ASR_API_KEY", ""),
            include_content_type=False,
        )
        data = {"model": model, "response_format": "json"}
        language = str(getattr(self.config, "MEDIA_ASR_LANGUAGE", "") or "").strip()
        if language:
            data["language"] = language
        with self._prepared_audio(file_path) as audio_path:
            with open(audio_path, "rb") as audio_file:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    data=data,
                    files={
                        "file": (
                            os.path.basename(audio_path),
                            audio_file,
                            mimetypes.guess_type(audio_path)[0] or "application/octet-stream",
                        )
                    },
                    timeout=float(getattr(self.config, "MEDIA_ASR_TIMEOUT", 180)),
                )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text}
        return self._normalize_text(self._response_text(payload), "MEDIA_ASR_MAX_CHARS")

    @staticmethod
    def _post_json(endpoint: str, payload: dict[str, Any], api_key: str, timeout: float) -> dict:
        import requests

        response = requests.post(
            endpoint,
            headers=MediaTextExtractor._auth_headers(api_key),
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("media model returned a non-object JSON response")
        return data

    @staticmethod
    def _auth_headers(api_key: str, include_content_type: bool = True) -> dict[str, str]:
        headers = {"Content-Type": "application/json"} if include_content_type else {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _endpoint(base_url: str, suffix: str) -> str:
        base_url = base_url.rstrip("/")
        return f"{base_url}{suffix}" if base_url.endswith("/v1") else f"{base_url}/v1{suffix}"

    def _provider(self, config_name: str, default: str) -> str:
        value = getattr(self.config, config_name, None)
        if value is None:
            value = os.getenv(config_name)
        return str(value or default).strip().lower()

    @staticmethod
    def _response_text(payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return ""
        if isinstance(payload.get("text"), str):
            return payload["text"]
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("text")
            )
        return str(content or "")

    def _normalize_text(self, text: str, limit_config_name: str) -> str:
        text = str(text or "").replace("\x00", "").strip()
        if not text:
            return ""
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        text = "\n".join(line for line in lines if line).strip()
        limit = int(getattr(self.config, limit_config_name, 4000))
        return text[:limit].rstrip() if limit > 0 else text

    @contextmanager
    def _prepared_audio(self, file_path: str):
        """Convert QQ SILK/AMR files when ffmpeg is available, then clean up."""

        suffix = os.path.splitext(file_path)[1].lower()
        convertible = {".silk", ".slk", ".amr"}
        should_convert = (
            suffix in convertible
            and bool(getattr(self.config, "MEDIA_ASR_CONVERT_AUDIO", True))
        )
        if not should_convert:
            yield file_path
            return

        temp_dir = str(getattr(self.config, "MEDIA_ASR_TEMP_DIR", "") or "").strip()
        if not temp_dir:
            temp_dir = os.path.join(
                str(getattr(self.config, "LOCAL_STORAGE", tempfile.gettempdir())),
                "tmp",
            )
        os.makedirs(temp_dir, exist_ok=True)
        fd, converted_path = tempfile.mkstemp(prefix="asr_", suffix=".wav", dir=temp_dir)
        os.close(fd)
        command = [
            str(getattr(self.config, "MEDIA_ASR_FFMPEG_BIN", "ffmpeg")),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            file_path,
            "-ar",
            "16000",
            "-ac",
            "1",
            converted_path,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=float(getattr(self.config, "MEDIA_ASR_CONVERT_TIMEOUT", 60)),
            )
            if result.returncode != 0 or not os.path.isfile(converted_path):
                detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
                raise RuntimeError(f"audio conversion failed: {detail[-500:]}")
            yield converted_path
        finally:
            try:
                os.unlink(converted_path)
            except FileNotFoundError:
                pass


def append_media_text(parsed: dict, extracted: list[tuple[dict, MediaTextResult]]) -> None:
    """Merge media text while keeping image OCR out of the visible message body."""

    additions = []
    plain_parts = [str(parsed.get("plain_text_content") or "").strip()]
    audio_parts = [str(parsed.get("audio_transcript") or "").strip()]
    for file_info, result in extracted:
        if not result.text:
            continue
        media_type = file_info.get("type")
        if media_type == "image":
            file_info["ocr_text"] = result.text
            plain_parts.append(result.text)
        elif media_type in ("audio", "record"):
            file_info["transcript"] = result.text
            additions.append(f"[语音转写]\n{result.text}")
            plain_parts.append(result.text)
            audio_parts.append(result.text)

    if additions:
        existing = str(parsed.get("message_content") or "").strip()
        parsed["message_content"] = "\n".join(part for part in (existing, *additions) if part).strip()
    parsed["plain_text_content"] = " ".join(part for part in plain_parts if part).strip()
    parsed["audio_transcript"] = "\n".join(part for part in audio_parts if part).strip()
