"""Persistent, token-aware group chat context compression."""

import json
import logging

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Estimate token count for mixed Chinese/English and punctuation."""
    text = str(text or "")
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    # Chinese chars roughly 1 token, English/symbols roughly 0.4 tokens
    return max(1, int(chinese * 1.0 + (len(text) - chinese) * 0.4) + 4)


def message_tokens(row: dict) -> int:
    content = row.get("message_content") or row.get("plain_text_content") or ""
    total = estimate_tokens(content) + 20
    attachments = row.get("attachments_json")
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except json.JSONDecodeError:
            attachments = []
    for attachment in attachments or []:
        if attachment.get("type") == "image":
            total += 765
        elif attachment.get("type") in ("audio", "record"):
            total += 500
    return total


class ContextManager:
    """Apply summary compression to one group context."""

    MAX_SUMMARY_CHUNK_TOKENS = 2500
    MAX_HISTORY_TOKENS = 1600
    MAX_RECENT_MESSAGES = 12

    def __init__(self, config, db, llm_client, summary_prompt: str, llm_options: dict | None = None):
        self.config = config
        self.db = db
        self.llm_client = llm_client
        self.summary_prompt = summary_prompt
        self.llm_options = llm_options or {}
        self.max_history_tokens = int(
            getattr(config, "CHAT_HISTORY_MAX_TOKENS", self.MAX_HISTORY_TOKENS)
        )
        self.max_recent_messages = int(
            getattr(config, "CHAT_HISTORY_MAX_MESSAGES", self.MAX_RECENT_MESSAGES)
        )

    def should_compress(self, rows: list[dict], system_prompt: str, user_prompt: str | list) -> bool:
        user_prompt_str = user_prompt if isinstance(user_prompt, str) else str(user_prompt)
        used = estimate_tokens(system_prompt) + estimate_tokens(user_prompt_str)
        return used >= self.config.CONTEXT_MAX_TOKENS * self.config.CONTEXT_SUMMARY_TRIGGER_RATIO

    def clip_recent_rows(
        self,
        rows: list[dict],
        max_tokens: int | None = None,
        max_count: int | None = None,
    ) -> list[dict]:
        """Keep the most recent rows within a safe token and count budget."""
        if not rows:
            return []
        token_budget = max_tokens or self.max_history_tokens
        count_limit = max_count or self.max_recent_messages
        used = 0
        kept = []
        for row in reversed(rows):
            tok = message_tokens(row)
            if kept and (used + tok > token_budget or len(kept) >= count_limit):
                break
            used += tok
            kept.append(row)
        kept.reverse()
        return kept

    def split_for_summary(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        """Split rows into old_rows (for summarization) and recent_rows (to keep in active prompt)."""
        if len(rows) < 2:
            return [], rows
        recent_rows = self.clip_recent_rows(rows)
        if len(recent_rows) >= len(rows):
            return [], rows
        old_rows = rows[: len(rows) - len(recent_rows)]
        return old_rows, recent_rows

    async def compress_if_needed(
        self,
        *,
        group_id: int,
        rows: list[dict],
        existing_summary: dict | None,
        system_prompt: str,
        user_prompt: str | list,
    ) -> tuple[dict | None, list[dict]]:
        if not self.should_compress(rows, system_prompt, user_prompt):
            return existing_summary, rows

        old_rows, recent_rows = self.split_for_summary(rows)
        if not old_rows:
            return existing_summary, recent_rows

        summary_chunk = []
        chunk_tokens = 0
        for row in reversed(old_rows):
            tok = message_tokens(row)
            if summary_chunk and (chunk_tokens + tok > self.MAX_SUMMARY_CHUNK_TOKENS or len(summary_chunk) >= 60):
                break
            chunk_tokens += tok
            summary_chunk.append(row)
        summary_chunk.reverse()

        old_text = self._format_rows(summary_chunk)
        previous = (existing_summary or {}).get("summary_text") or "（暂无更早摘要）"
        summary_prompt = (
            f"[群号] {group_id}\n"
            "[已有上下文摘要]\n"
            f"{previous}\n\n"
            "[需要并入摘要的新增群聊记录]\n"
            f"{old_text}\n\n"
            "请根据系统提示词更新群聊上下文摘要。只输出摘要正文。"
        )

        self.db.mark_context_summary_status(group_id, "summarizing")
        try:
            summary_text = await self.llm_client.generate_text(
                system_prompt=self.summary_prompt,
                user_prompt=summary_prompt,
                max_tokens=self.config.CONTEXT_SUMMARY_MAX_TOKENS,
                temperature=0.2,
                **self.llm_options,
            )
            if not summary_text.strip():
                raise RuntimeError("summary model returned empty content")
        except Exception:
            logger.exception(
                "Context summary failed for group %s; falling back to safe clipped recent context",
                group_id,
            )
            self.db.mark_context_summary_status(group_id, "failed")
            return existing_summary, recent_rows

        boundary_id = int(old_rows[-1]["_db_id"])
        summary = self.db.save_context_summary(group_id, summary_text.strip(), boundary_id)
        logger.info(
            "Context summary completed for group %s through db message %s; kept %s exact messages",
            group_id,
            boundary_id,
            len(recent_rows),
        )
        return summary, recent_rows

    @staticmethod
    def _format_rows(rows: list[dict]) -> str:
        lines = []
        for row in rows:
            sender = "云萤" if row.get("is_self") else str(row.get("sender_id"))
            content = row.get("message_content") or row.get("plain_text_content") or ""
            if len(content) > 6000:
                content = content[:6000] + "..."
            if row.get("attachments_json"):
                content = f"{content} [包含附件/图片]"
            lines.append(f"[{row.get('msg_id')}] {sender}: {content}")
        return "\n".join(lines)
