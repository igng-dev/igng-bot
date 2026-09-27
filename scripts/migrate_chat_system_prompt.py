#!/usr/bin/env python3
"""Migrate the canonical chat prompt into system_prompts.chat.

This is intentionally an explicit operation.  Normal application startup only
seeds a missing row and never overwrites the database value.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from igngbot_v3.config import Config
from igngbot_v3.db import DBHandler
from igngbot_v3.system_prompt_store import SystemPromptStore


CREATE_SYSTEM_PROMPTS_SQL = """
CREATE TABLE IF NOT EXISTS system_prompts (
    prompt_key VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '提示词类型: chat',
    prompt_text MEDIUMTEXT NOT NULL COMMENT '完整聊天系统提示词',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='完整聊天系统提示词配置'
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompt-file",
        default=str(PROJECT_ROOT / "prompts" / "system.txt"),
        help="Canonical prompt file to migrate (default: prompts/system.txt)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the prompt to system_prompts.chat; without this flag the command is a dry run.",
    )
    parser.add_argument(
        "--expected-sha256",
        help="Expected SHA-256 of the current database prompt. Required for a guarded overwrite.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow overwriting a non-empty database prompt without an expected hash.",
    )
    return parser.parse_args()


def read_prompt(path: Path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    valid, reason = SystemPromptStore.validate_prompt(prompt)
    if not valid:
        raise ValueError(f"canonical prompt is invalid: {reason}")
    return prompt


def ensure_table(db: DBHandler) -> None:
    with db.conn.cursor() as cursor:
        cursor.execute(CREATE_SYSTEM_PROMPTS_SQL)
    db.conn.commit()


def main() -> int:
    args = parse_args()
    prompt_path = Path(args.prompt_file).expanduser().resolve()
    try:
        target_prompt = read_prompt(prompt_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    config = Config()
    db = DBHandler(config)
    try:
        db.connect()
        # Dry-run must not create or alter tables. The existing deployment is
        # expected to have system_prompts already; only an applying migration
        # is allowed to repair/create that table.
        if args.apply:
            ensure_table(db)
        current = db.get_system_prompt_record("chat")
        current_prompt = (current.get("prompt_text") or "").strip()
        current_hash = SystemPromptStore.fingerprint(current_prompt) if current_prompt else ""
        target_hash = SystemPromptStore.fingerprint(target_prompt)

        print(f"prompt_file={prompt_path}")
        print(f"current_sha256={current_hash or '(missing)'}")
        print(f"target_sha256={target_hash}")
        print(f"current_updated_at={current.get('updated_at') or '(missing)'}")

        if current_hash == target_hash:
            print("No migration needed: database already contains the target prompt.")
            return 0
        if not args.apply:
            print("Dry run only. Re-run with --apply to write the target prompt.")
            return 0
        if current_prompt and not args.force:
            if not args.expected_sha256:
                print(
                    "ERROR: refusing to overwrite a non-empty database prompt; "
                    "provide --expected-sha256 or --force.",
                    file=sys.stderr,
                )
                return 3
            if args.expected_sha256.lower() != current_hash.lower():
                print(
                    "ERROR: database prompt hash changed; refusing to overwrite it.",
                    file=sys.stderr,
                )
                return 4

        backup_dir = Path(config.LOCAL_STORAGE) / "system_prompt_migrations"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / (
            f"chat-before-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        backup_path.write_text(
            json.dumps(
                {
                    "prompt_key": "chat",
                    "prompt_text": current_prompt,
                    "created_at": current.get("created_at"),
                    "updated_at": current.get("updated_at"),
                    "sha256": current_hash,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        db.set_system_prompt("chat", target_prompt)
        print(f"Migration applied. Backup: {backup_path}")
        return 0
    finally:
        if db._conn is not None:
            db._conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
