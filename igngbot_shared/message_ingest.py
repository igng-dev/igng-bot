"""Shared V3/V4 ingress: parse, store media, and persist raw QQ history.
No chat model, reply classifier, context compression or media transcription
runs here.
"""
import asyncio
import json
import logging
from .message_parser import parse_message, apply_forward_content
from .onebot_api import get_forward_msg
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
    # task handling so continuous task mode can use stable NAS paths and the
    # multimodal model can read the stored media.
    legacy_compat = getattr(self.db, "legacy_compat", True)
    file_url = None
    file_type = None
    audio_file_path = None
    stored_attachments = []
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
                attachment = {
                    "type": file_info["type"],
                    "original_file": file_info.get("file", ""),
                    "original_url": file_info.get("url", ""),
                    "stored_path": stored_path,
                    "thumb_path": thumb_path,
                    "name": file_info.get("name", ""),
                }
                if meta.get("width"):
                    attachment["width"] = meta["width"]
                    attachment["height"] = meta.get("height")
                if meta.get("thumb_width"):
                    attachment["thumb_width"] = meta["thumb_width"]
                    attachment["thumb_height"] = meta["thumb_height"]
                if "is_animated" in meta:
                    attachment["is_animated"] = meta["is_animated"]
                stored_attachments.append(attachment)
                if legacy_compat and file_url is None:
                    file_url = stored_path
                    file_type = file_info["type"]
                if legacy_compat and file_info["type"] in ("audio", "record") and audio_file_path is None:
                    audio_file_path = stored_path

    # Keep the message handler usable in lightweight compensation/test
    # contexts where only the database dependency is constructed. The normal
    # App initializer always provides storage; without it there is simply no
    # nested card/forward media to hydrate.
    storage = getattr(self, "storage", None)
    nested_media_text = []
    if storage is not None:
        nested_media_text = await hydrate_structure_media(
            storage,
            parsed.get("message_structure", []),
            parsed["group_id"],
            parsed["msg_id"],
        )
    if nested_media_text:
        parsed["plain_text_content"] = " ".join(
            part
            for part in (parsed.get("plain_text_content", ""), *nested_media_text)
            if str(part).strip()
        ).strip()

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
            created_at=(
                unix_to_utc_naive(parsed["created_at"])
                if parsed.get("created_at")
                else None
            ),
            is_self=parsed.get("is_self", False),
            message_source=parsed.get("message_source"),
            audio_transcript=parsed.get("audio_transcript"),
            **({"file_url": file_url, "file_type": file_type, "audio_file_path": audio_file_path} if legacy_compat else {}),
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
