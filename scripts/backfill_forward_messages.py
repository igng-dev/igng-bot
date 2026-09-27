#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igngbot_v3.config import Config
from igngbot_v3.db import DBHandler
from igngbot_v3.media_text import MediaTextExtractor
from igngbot_v3.message_media import hydrate_structure_media
from igngbot_v3.message_parser import apply_forward_content, normalize_message_structure
from igngbot_v3.onebot_api import get_forward_msg
from igngbot_v3.storage import StorageHandler


logger = logging.getLogger("backfill_forward_messages")


def pending_forward_ids(structure):
    if not isinstance(structure, list):
        return []
    return [
        str(part.get("id"))
        for part in structure
        if isinstance(part, dict)
        and part.get("type") == "forward"
        and part.get("id")
        and not part.get("children")
    ]


def _is_media_part(part):
    return isinstance(part, dict) and part.get("type") in {
        "image",
        "mface",
        "face",
        "audio",
        "record",
    }


def _parts_contain_media(parts):
    if not isinstance(parts, list):
        return False
    for part in parts:
        if _is_media_part(part):
            return True
        if isinstance(part, dict) and part.get("type") in {"forward", "node"}:
            if any(_parts_contain_media(child.get("children")) for child in part.get("children", []) if isinstance(child, dict)):
                return True
    return False


def _forward_needs_refresh(part):
    children = part.get("children") if isinstance(part, dict) else None
    if not isinstance(children, list) or not children:
        return True
    for child in children:
        if not isinstance(child, dict):
            continue
        files = child.get("files")
        attachments = child.get("attachments")
        if _parts_contain_media(child.get("children")) and not attachments:
            return True
        if isinstance(files, list):
            for item in files:
                if not isinstance(item, dict) or item.get("type") not in {"image", "mface", "face", "audio", "record"}:
                    continue
                if item.get("url") and not item.get("stored_path"):
                    return True
        elif _parts_contain_media(child.get("children")):
            return True
    return False


def forward_refresh_ids(structure):
    if not isinstance(structure, list):
        return []
    result = []
    for part in structure:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "forward" and part.get("id") and _forward_needs_refresh(part):
            result.append(str(part["id"]))
        for child in part.get("children", []):
            if isinstance(child, dict):
                result.extend(forward_refresh_ids(child.get("children")))
    return list(dict.fromkeys(result))


def strip_legacy_image_ocr(content):
    lines = str(content or "").splitlines()
    output = []
    skipping = False
    for line in lines:
        marker = line.strip()
        if marker == "[图片OCR]":
            skipping = True
            continue
        if skipping and marker.startswith("[") and marker != "[图片OCR]":
            skipping = False
        if not skipping:
            output.append(line)
    return "\n".join(output).strip()


def strip_legacy_card_payload(content):
    return re.sub(
        r"\[(卡片|小程序):[^\r\n]*?\]",
        lambda match: f"[{match.group(1)}]",
        str(content or ""),
    ).strip()


async def backfill(config, db, limit, dry_run, after_id=0):
    with db.conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, group_id, message_content, plain_text_content, message_structure
            FROM message_logs
            WHERE (
                message_content LIKE %s
                OR message_structure LIKE %s
                OR message_structure LIKE %s
                OR message_structure LIKE %s
            )
              AND id > %s
            ORDER BY id ASC
            LIMIT %s
            """,
            ("%[图片OCR]%", "%forward%", "%card%", "%json%", int(after_id), int(limit)),
        )
        rows = list(cursor.fetchall())

    storage = None if dry_run else StorageHandler(config)
    media_text = None if dry_run else MediaTextExtractor(config)
    if storage is not None:
        storage.check_available()

    scanned = expanded = hydrated = cleaned = failed = 0
    for row in rows:
        scanned += 1
        try:
            structure = json.loads(row.get("message_structure") or "[]")
        except (TypeError, json.JSONDecodeError):
            failed += 1
            continue
        parsed = {
            "message_content": row.get("message_content") or "",
            "plain_text_content": row.get("plain_text_content") or "",
            "message_structure": structure,
        }
        changed = normalize_message_structure(structure)
        pending_ids = set(pending_forward_ids(structure))
        forward_ids = list(dict.fromkeys([*pending_ids, *forward_refresh_ids(structure)]))
        for forward_id in forward_ids:
            messages = await get_forward_msg(config, forward_id)
            if apply_forward_content(
                parsed,
                messages,
                forward_id=forward_id,
                force=forward_id not in pending_ids,
            ):
                changed = True

        if storage is not None:
            before_media_structure = json.dumps(parsed["message_structure"], ensure_ascii=False, sort_keys=True)
            nested_media_text = await hydrate_structure_media(
                storage,
                media_text,
                parsed["message_structure"],
                row["group_id"],
                row["id"],
            )
            after_media_structure = json.dumps(parsed["message_structure"], ensure_ascii=False, sort_keys=True)
            if before_media_structure != after_media_structure:
                hydrated += 1
                changed = True
            if nested_media_text:
                parsed["plain_text_content"] = " ".join(
                    part
                    for part in (parsed.get("plain_text_content", ""), *nested_media_text)
                    if str(part).strip()
                ).strip()

        cleaned_content = strip_legacy_image_ocr(parsed["message_content"])
        cleaned_content = strip_legacy_card_payload(cleaned_content)
        if cleaned_content != parsed["message_content"]:
            parsed["message_content"] = cleaned_content
            parsed["plain_text_content"] = re.sub(r"\s+", " ", cleaned_content).strip()
            cleaned += 1
            changed = True

        if not changed:
            continue

        expanded += 1
        if dry_run:
            logger.info("would update message_logs.id=%s", row["id"])
            continue
        with db.conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE message_logs
                SET message_content = %s,
                    plain_text_content = %s,
                    message_structure = %s
                WHERE id = %s
                """,
                (
                    parsed["message_content"],
                    parsed["plain_text_content"],
                    json.dumps(parsed["message_structure"], ensure_ascii=False),
                    row["id"],
                ),
            )
        db.conn.commit()

    return {
        "scanned": scanned,
        "expanded": expanded,
        "hydrated": hydrated,
        "cleaned": cleaned,
        "failed": failed,
        "next_after_id": int(rows[-1]["id"]) if rows else int(after_id),
    }


def main():
    parser = argparse.ArgumentParser(description="Expand stored QQ forwarded-message placeholders")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    db = DBHandler(Config)
    db.connect()
    try:
        result = asyncio.run(
            backfill(
                Config,
                db,
                max(1, args.limit),
                args.dry_run,
                max(0, args.after_id),
            )
        )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if db._conn is not None:
            db._conn.close()


if __name__ == "__main__":
    main()
