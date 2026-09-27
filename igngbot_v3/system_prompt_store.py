"""Database-backed local cache for the chat system prompt.

The database row is the authoritative editable value.  Message handling never
queries it directly; it reads the last validated local snapshot maintained by
this module instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock

logger = logging.getLogger(__name__)


class SystemPromptStore:
    """Load, validate, cache, and periodically refresh one system prompt."""

    REQUIRED_MARKERS = (
        "# 角色设定",
        "# 固定人格：亲和",
        "# 道德与风险言论约束",
        "# 输出格式",
        '"should_reply"',
        '"reply_text"',
    )

    def __init__(self, config, db, prompt_key: str = "chat"):
        self.config = config
        self.db = db
        self.prompt_key = str(prompt_key)
        cache_dir = getattr(config, "SYSTEM_PROMPT_CACHE_DIR", "")
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path(config.LOCAL_STORAGE) / "system_prompts"
        self.cache_path = self.cache_dir / f"{self.prompt_key}.txt"
        self.meta_path = self.cache_dir / f"{self.prompt_key}.meta.json"
        self.bootstrap_path = Path(config.PROMPT_DIR) / "system.txt"
        self._lock = RLock()
        self._prompt = ""
        self._metadata: dict = {}
        self._sync_task: asyncio.Task | None = None

    @staticmethod
    def fingerprint(prompt_text: str) -> str:
        return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    @classmethod
    def validate_prompt(cls, prompt_text: str) -> tuple[bool, str]:
        normalized = (prompt_text or "").strip()
        if not normalized:
            return False, "提示词为空"
        missing = [marker for marker in cls.REQUIRED_MARKERS if marker not in normalized]
        if missing:
            return False, f"缺少必需段落或输出字段: {', '.join(missing)}"
        return True, ""

    @staticmethod
    def _json_value(value):
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        if isinstance(value, date):
            return value.isoformat()
        return value

    def _read_text_file(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _set_memory(self, prompt_text: str, metadata: dict | None = None) -> None:
        with self._lock:
            self._prompt = prompt_text.strip()
            self._metadata = dict(metadata or {})

    def _write_metadata(self, metadata: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.meta_path.with_name(
            f".{self.meta_path.name}.{os.getpid()}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.meta_path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _write_snapshot(
        self,
        prompt_text: str,
        *,
        source: str,
        db_updated_at=None,
    ) -> None:
        normalized = prompt_text.strip()
        prompt_hash = self.fingerprint(normalized)
        metadata = {
            "prompt_key": self.prompt_key,
            "sha256": prompt_hash,
            "source": source,
            "db_updated_at": self._json_value(db_updated_at),
            "synced_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.cache_path.with_name(
            f".{self.cache_path.name}.{os.getpid()}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                handle.write(normalized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.cache_path)
            self._write_metadata(metadata)
            self._set_memory(normalized, metadata)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def load_local(self) -> bool:
        """Load a previously synced snapshot, bootstrapping from source if needed."""
        local_prompt = self._read_text_file(self.cache_path)
        valid, reason = self.validate_prompt(local_prompt)
        if valid:
            metadata = {}
            try:
                metadata = json.loads(self.meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {
                    "prompt_key": self.prompt_key,
                    "sha256": self.fingerprint(local_prompt),
                    "source": "local-cache",
                }
            self._set_memory(local_prompt, metadata)
            logger.info(
                "Loaded local system prompt: key=%s sha256=%s source=%s",
                self.prompt_key,
                metadata.get("sha256", self.fingerprint(local_prompt)),
                metadata.get("source", "local-cache"),
            )
            return True
        if local_prompt:
            logger.warning(
                "Ignoring invalid local system prompt cache: key=%s reason=%s",
                self.prompt_key,
                reason,
            )

        bootstrap_prompt = self._read_text_file(self.bootstrap_path)
        valid, reason = self.validate_prompt(bootstrap_prompt)
        if not valid:
            if bootstrap_prompt:
                logger.error(
                    "Ignoring invalid bootstrap system prompt: path=%s reason=%s",
                    self.bootstrap_path,
                    reason,
                )
            return False
        try:
            self._write_snapshot(bootstrap_prompt, source="bootstrap")
        except OSError:
            # The in-memory copy still provides a valid fallback if the local
            # runtime directory cannot be written during startup.
            self._set_memory(
                bootstrap_prompt,
                {
                    "prompt_key": self.prompt_key,
                    "sha256": self.fingerprint(bootstrap_prompt),
                    "source": "bootstrap-memory",
                },
            )
            logger.exception("Failed to persist bootstrap system prompt cache")
        logger.info("Bootstrapped local system prompt from %s", self.bootstrap_path)
        return True

    def get(self, prompt_key: str | None = None) -> str:
        requested_key = self.prompt_key if prompt_key is None else str(prompt_key)
        if requested_key != self.prompt_key:
            return ""
        with self._lock:
            return self._prompt

    def metadata(self) -> dict:
        with self._lock:
            return dict(self._metadata)

    async def sync_once(self) -> bool:
        """Pull and validate the authoritative database prompt once."""
        try:
            row_reader = getattr(self.db, "get_system_prompt_record", None)
            if row_reader is not None:
                row = row_reader(self.prompt_key) or {}
                prompt_text = row.get("prompt_text", "")
                db_updated_at = row.get("updated_at")
            else:
                prompt_text = self.db.get_system_prompt(self.prompt_key)
                db_updated_at = None
                row = {}
        except Exception:
            logger.exception("Failed to pull system prompt from database: key=%s", self.prompt_key)
            return False

        valid, reason = self.validate_prompt(prompt_text)
        if not valid:
            logger.error(
                "Rejected invalid database system prompt: key=%s reason=%s",
                self.prompt_key,
                reason,
            )
            return False

        normalized = prompt_text.strip()
        prompt_hash = self.fingerprint(normalized)
        current_hash = self.metadata().get("sha256")
        if current_hash == prompt_hash and self.get(self.prompt_key) == normalized:
            return True

        try:
            self._write_snapshot(
                normalized,
                source="database",
                db_updated_at=db_updated_at,
            )
        except OSError:
            logger.exception("Failed to write local system prompt cache: path=%s", self.cache_path)
            return False

        logger.info(
            "Synchronized local system prompt: key=%s sha256=%s db_updated_at=%s",
            self.prompt_key,
            prompt_hash,
            db_updated_at,
        )
        return True

    async def initialize(self) -> None:
        """Load a usable local prompt, then prefer the database value."""
        self.load_local()
        await self.sync_once()
        if not self.get(self.prompt_key):
            raise RuntimeError(
                f"No valid local system prompt is available for key={self.prompt_key}"
            )

    def start(self) -> None:
        if self._sync_task is not None and not self._sync_task.done():
            return
        interval = float(
            getattr(self.config, "SYSTEM_PROMPT_SYNC_INTERVAL_SECONDS", 60.0)
        )
        if interval <= 0:
            logger.info("System prompt periodic synchronization is disabled")
            return
        self._sync_task = asyncio.create_task(
            self._run_periodic(interval),
            name=f"system-prompt-sync-{self.prompt_key}",
        )
        logger.info(
            "Started system prompt synchronization: key=%s interval=%ss",
            self.prompt_key,
            interval,
        )

    async def _run_periodic(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Unexpected system prompt synchronization failure: key=%s",
                    self.prompt_key,
                )

    async def stop(self) -> None:
        task = self._sync_task
        self._sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
