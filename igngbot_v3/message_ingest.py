"""Shared V3/V4 ingress: parse, store media, OCR/ASR, and persist raw QQ history.
No chat model, reply classifier or context compression runs here.
"""
import asyncio
import json
import logging
from .message_parser import parse_message, apply_forward_content
from .onebot_api import get_forward_msg
from .media_text import MediaTextResult, append_media_text
from .message_media import hydrate_structure_media
from .timeutil import unix_to_utc_naive
logger = logging.getLogger(__name__)

def _json_dump(value):
    return json.dumps(value, ensure_ascii=False)

async def persist_message(self, data):
    parsed = parse_message(data)
    if parsed is None:
        return
    # OneBot should always provide message_id for message events. Do not
    # send an empty ID to the database: it would fail the schema contract
    # and could make malformed self events look like duplicate messages.
    if not str(parsed.get("msg_id") or "").strip():
        logger.warning(
            "Ignoring message event without message_id: type=%s conversation=%s self=%s",
            parsed.get("conversation_type"),
            parsed.get("group_id"),
            parsed.get("is_self"),
        )
        return

    forward_id = str(parsed.get("_forward_id") or "").strip()
    if forward_id and not parsed.get("_forward_content"):
        forward_content = await get_forward_msg(self.config, forward_id)
        if forward_content:
            apply_forward_content(parsed, forward_content, forward_id=forward_id)

    if parsed.get("conversation_type") == "group":
        self.db.ensure_group_exists(parsed["group_id"])
    if parsed.get("is_self") and self.db.get_message_by_msg_id(
        parsed["group_id"], parsed["msg_id"]
    ):
        return
    # Persist incoming messages and download attachments before command or
    # task handling so continuous task mode can use stable NAS paths and
    # media-derived text can be included in the same LLM turn.
    file_url = None
    file_type = None
    audio_file_path = None
    stored_attachments = []
    extracted_media = []
    if parsed.get("files"):
        for file_info in parsed["files"]:
            storage_res = None
            if file_info.get("type") == "face":
                face_id = file_info.get("id")
                if face_id:
                    storage_res = await asyncio.to_thread(
                        self.storage.store_face_if_missing,
                        face_id,
                        file_info.get("url", ""),
                    )
            elif file_info.get("url"):
                name = file_info.get("file") or file_info.get("name", "unknown")
                file_name = f"{parsed['msg_id']}_{name}"
                storage_res = await asyncio.to_thread(
                    self.storage.download_and_store,
                    file_info["url"],
                    parsed["group_id"],
                    file_name,
                    file_info["type"],
                )
            else:
                continue

            if not storage_res:
                continue

            if isinstance(storage_res, dict):
                stored_path = storage_res.get("stored_path")
                thumb_path = storage_res.get("thumb_path")
                meta = storage_res.get("meta") or {}
            else:
                stored_path = str(storage_res)
                thumb_path = None
                meta = {}

            if stored_path:
                file_info["stored_path"] = stored_path
                if thumb_path:
                    file_info["thumb_path"] = thumb_path
                media_result = None
                try:
                    if file_info["type"] == "image":
                        media_result = await asyncio.to_thread(
                            self.media_text.extract,
                            file_info["type"],
                            stored_path,
                        )
                    elif file_info["type"] in ("audio", "record"):
                        existing_transcript = str(file_info.get("transcript") or "").strip()
                        if existing_transcript:
                            media_result = MediaTextResult(
                                text=existing_transcript,
                                status="provided",
                                backend="onebot",
                            )
                        else:
                            media_result = await asyncio.to_thread(
                                self.media_text.extract,
                                file_info["type"],
                                stored_path,
                            )
                except Exception as exc:
                    logger.exception("Media text extraction failed for %s", stored_path)
                    media_result = MediaTextResult(
                        status="unavailable",
                        backend="error",
                        error=str(exc),
                    )
                if media_result:
                    file_info["text_extraction_status"] = media_result.status
                    file_info["text_extraction_backend"] = media_result.backend
                    if media_result.text and media_result.backend != "onebot":
                        if file_info["type"] == "image":
                            file_info["ocr_text"] = media_result.text
                        else:
                            file_info["transcript"] = media_result.text
                        extracted_media.append((file_info, media_result))
                attachment = {
                    "type": file_info["type"],
                    "original_file": file_info.get("file", ""),
                    "original_url": file_info.get("url", ""),
                    "stored_path": stored_path,
                    "thumb_path": thumb_path,
                    "name": file_info.get("name", ""),
                    "transcript": file_info.get("transcript", ""),
                    "ocr_text": file_info.get("ocr_text", ""),
                }
                if meta.get("width"):
                    attachment["width"] = meta["width"]
                    attachment["height"] = meta.get("height")
                if meta.get("thumb_width"):
                    attachment["thumb_width"] = meta["thumb_width"]
                    attachment["thumb_height"] = meta.get("thumb_height")
                if "is_animated" in meta:
                    attachment["is_animated"] = meta["is_animated"]

                if media_result:
                    attachment["text_extraction_status"] = media_result.status
                    attachment["text_extraction_backend"] = media_result.backend
                stored_attachments.append(attachment)
                if file_url is None:
                    file_url = stored_path
                    file_type = file_info["type"]
                if file_info["type"] in ("audio", "record") and audio_file_path is None:
                    audio_file_path = stored_path

    append_media_text(parsed, extracted_media)
    # Keep the message handler usable in lightweight compensation/test
    # contexts where only the database dependency is constructed.  The
    # normal App initializer always provides both services; without them
    # there is simply no nested card/forward media to hydrate.
    storage = getattr(self, "storage", None)
    media_text = getattr(self, "media_text", None)
    if storage is not None and media_text is not None:
        nested_media_text = await hydrate_structure_media(
            storage,
            media_text,
            parsed.get("message_structure", []),
            parsed["group_id"],
            parsed["msg_id"],
        )
    else:
        nested_media_text = []
    if nested_media_text:
        parsed["plain_text_content"] = " ".join(
            part
            for part in (parsed.get("plain_text_content", ""), *nested_media_text)
            if str(part).strip()
        ).strip()
    content = parsed.get("message_content", "").strip()

    if not self.db.get_message_by_msg_id(parsed["group_id"], parsed["msg_id"]):
        self.db.insert_message(
            group_id=parsed["group_id"],
            sender_id=parsed["sender_id"],
            message_content=parsed["message_content"],
            plain_text_content=parsed.get("plain_text_content", ""),
            message_structure=_json_dump(parsed.get("message_structure", [])),
            attachments_json=_json_dump(stored_attachments) if stored_attachments else None,
            reply_to_msg_id=parsed["reply_to_msg_id"],
            msg_id=parsed["msg_id"],
            file_url=file_url,
            file_type=file_type,
            created_at=(
                unix_to_utc_naive(parsed["created_at"])
                if parsed.get("created_at")
                else None
            ),
            is_self=parsed.get("is_self", False),
            message_source=parsed.get("message_source"),
            audio_file_path=audio_file_path,
            audio_transcript=parsed.get("audio_transcript", ""),
        )

    stored_message = self.db.get_message_by_msg_id(parsed["group_id"], parsed["msg_id"])
    if stored_message and stored_message.get("is_recalled"):
        logger.info(
            "Skipping recalled message after persistence: group=%s msg=%s",
            parsed["group_id"],
            parsed["msg_id"],
        )
        return

    return parsed
