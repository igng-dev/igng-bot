import asyncio
import os
import re

from .media_text import MediaTextResult


def _safe_filename(value, fallback="media"):
    name = os.path.basename(str(value or "").strip())
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("._")
    return name or fallback


def _attachment_from_result(file_info, stored_path, thumb_path, meta, media_result):
    attachment = {
        "type": file_info.get("type", "image"),
        "original_file": file_info.get("file", ""),
        "original_url": file_info.get("url", ""),
        "stored_path": stored_path,
        "thumb_path": thumb_path,
        "name": file_info.get("name", ""),
        "transcript": file_info.get("transcript", ""),
        "ocr_text": file_info.get("ocr_text", ""),
    }
    role = str(file_info.get("role") or "").strip()
    if role:
        attachment["role"] = role
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
    return attachment


async def persist_media_file(storage, media_text, file_info, group_id, file_name):
    if not isinstance(file_info, dict):
        return None

    file_type = str(file_info.get("type") or "image").strip() or "image"
    storage_res = None
    stored_path = file_info.get("stored_path")
    thumb_path = file_info.get("thumb_path")
    meta = file_info.get("meta") or {}

    if stored_path:
        storage_res = {
            "stored_path": stored_path,
            "thumb_path": thumb_path,
            "meta": meta,
        }
    elif file_type == "face" and file_info.get("id"):
        storage_res = await asyncio.to_thread(
            storage.store_face_if_missing,
            file_info.get("id"),
            file_info.get("url", ""),
        )
    elif file_info.get("url"):
        storage_res = await asyncio.to_thread(
            storage.download_and_store,
            file_info["url"],
            group_id,
            _safe_filename(file_name, file_type),
            "mface" if file_type == "mface" else file_type,
        )

    if not storage_res:
        return None
    if isinstance(storage_res, dict):
        stored_path = storage_res.get("stored_path")
        thumb_path = storage_res.get("thumb_path")
        meta = storage_res.get("meta") or {}
    else:
        stored_path = str(storage_res)
        thumb_path = None
        meta = {}
    if not stored_path:
        return None

    file_info["stored_path"] = stored_path
    if thumb_path:
        file_info["thumb_path"] = thumb_path

    media_result = None
    try:
        if file_type == "image":
            existing_ocr = str(file_info.get("ocr_text") or "").strip()
            if existing_ocr:
                media_result = MediaTextResult(existing_ocr, "provided", "stored")
            else:
                media_result = await asyncio.to_thread(media_text.extract, file_type, stored_path)
        elif file_type in ("audio", "record"):
            existing_transcript = str(file_info.get("transcript") or "").strip()
            if existing_transcript:
                media_result = MediaTextResult(existing_transcript, "provided", "onebot")
            else:
                media_result = await asyncio.to_thread(media_text.extract, file_type, stored_path)
    except Exception as exc:
        media_result = MediaTextResult(status="unavailable", backend="error", error=str(exc))

    if media_result and media_result.text and media_result.backend != "onebot":
        if file_type == "image":
            file_info["ocr_text"] = media_result.text
        elif file_type in ("audio", "record"):
            file_info["transcript"] = media_result.text

    attachment = _attachment_from_result(file_info, stored_path, thumb_path, meta, media_result)
    return {
        "attachment": attachment,
        "file_info": file_info,
        "media_result": media_result,
    }


async def hydrate_structure_media(storage, media_text, structure, group_id, message_id):
    if not isinstance(structure, list):
        return []

    plain_parts = []

    async def process_card(part, scope):
        if not isinstance(part, dict):
            return
        summary = str(part.get("summary") or part.get("title") or part.get("description") or "").strip()
        if summary:
            plain_parts.append(summary)
        media = part.get("media")
        if not isinstance(media, list):
            return
        attachments = []
        for index, media_item in enumerate(media):
            if not isinstance(media_item, dict):
                continue
            file_info = {
                **media_item,
                "type": "image",
                "name": media_item.get("role") or f"card_{index}",
            }
            persisted = await persist_media_file(
                storage,
                media_text,
                file_info,
                group_id,
                f"{message_id}_{scope}_card_{index}_{file_info.get('role', 'image')}",
            )
            if not persisted:
                continue
            media_item.update(
                {
                    "stored_path": file_info.get("stored_path"),
                    "thumb_path": file_info.get("thumb_path"),
                    "ocr_text": file_info.get("ocr_text", ""),
                }
            )
            attachment = persisted["attachment"]
            attachment["role"] = media_item.get("role", "image")
            attachments.append(attachment)
            if attachment.get("ocr_text"):
                plain_parts.append(str(attachment["ocr_text"]))
        if attachments:
            part["attachments"] = attachments

    async def process_forward_part(part, scope):
        if not isinstance(part, dict):
            return
        children = part.get("children")
        if not isinstance(children, list):
            return
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                continue
            child_scope = f"{scope}_{index}"
            child_attachments = []
            files = child.get("files")
            if isinstance(files, list):
                for file_index, file_info in enumerate(files):
                    if not isinstance(file_info, dict):
                        continue
                    persisted = await persist_media_file(
                        storage,
                        media_text,
                        file_info,
                        group_id,
                        f"{message_id}_{child_scope}_{file_index}_{file_info.get('file', file_info.get('type', 'media'))}",
                    )
                    if not persisted:
                        continue
                    child_attachments.append(persisted["attachment"])
                    attachment = persisted["attachment"]
                    if attachment.get("ocr_text"):
                        plain_parts.append(str(attachment["ocr_text"]))
                    if attachment.get("transcript"):
                        plain_parts.append(str(attachment["transcript"]))
            if child_attachments:
                child["attachments"] = child_attachments

            nested_parts = child.get("children")
            if not isinstance(nested_parts, list):
                continue
            for part_index, nested_part in enumerate(nested_parts):
                part_type = nested_part.get("type") if isinstance(nested_part, dict) else ""
                nested_scope = f"{child_scope}_{part_index}"
                if part_type in ("forward", "node"):
                    await process_forward_part(nested_part, nested_scope)
                elif part_type == "card":
                    await process_card(nested_part, nested_scope)

    for index, part in enumerate(structure):
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("forward", "node"):
            await process_forward_part(part, f"forward_{index}")
        elif part_type == "card":
            await process_card(part, f"card_{index}")

    return plain_parts
