"""Bounded model views. File access is derived only from this conversation's DB rows."""
import base64
import io
import json
from pathlib import Path
from PIL import Image


_REMOVED = {"raw_event", "stored_path", "thumb_path", "file_url", "audio_file_path", "original_url", "original_file", "path", "file", "url"}


def safe_structure(value, depth=0):
    if depth > 12:
        return "[嵌套内容超过展示限制]"
    if isinstance(value, list):
        return [safe_structure(v, depth + 1) for v in value[:100]]
    if isinstance(value, dict):
        return {k: safe_structure(v, depth + 1) for k, v in value.items() if k not in _REMOVED}
    if isinstance(value, str):
        return value[:16000]
    return value


def json_value(value, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return fallback


def message_view(row):
    recalled = bool(row.get("is_recalled"))
    return {"messageId": str(row["msg_id"]), "userId": str(row["sender_id"]),
            "isSelf": bool(row.get("is_self")), "isRecalled": recalled,
            "text": "[消息已撤回]" if recalled else (row.get("message_content") or "")[:16000],
            "plain": "" if recalled else (row.get("plain_text_content") or "")[:16000],
            "replyToMessageId": row.get("reply_to_msg_id"),
            "media": [] if recalled else safe_structure(json_value(row.get("attachments_json"), [])),
            "structure": [] if recalled else safe_structure(json_value(row.get("message_structure"), []))}


def row_images(row, storage, root, limit=3):
    if row.get("is_recalled"):
        return []
    attachments = json_value(row.get("attachments_json"), [])
    structure = json_value(row.get("message_structure"), [])
    candidates = []

    def collect(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, list):
            for child in value[:100]:
                collect(child, depth + 1)
        elif isinstance(value, dict):
            if value.get("type") in {"image", "mface", "face"} and value.get("stored_path"):
                candidates.append(value["stored_path"])
            for child in value.values():
                if isinstance(child, (list, dict)):
                    collect(child, depth + 1)
    collect(attachments)
    collect(structure)
    images = []
    for stored in dict.fromkeys(candidates):
        path = Path(storage.resolve_path(stored)).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file() or path.stat().st_size > 15 * 1024 * 1024:
            continue
        try:
            with Image.open(path) as image:
                if image.width * image.height > 40_000_000:
                    continue
                image.thumbnail((1280, 1280))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="WEBP", quality=82)
                images.append({"mediaType": "image/webp", "data": base64.b64encode(output.getvalue()).decode(),
                               "messageId": str(row["msg_id"])})
        except (OSError, ValueError):
            continue
        if len(images) >= min(3, max(1, limit)):
            break
    return images
