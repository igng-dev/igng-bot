"""Persistent, token-aware group chat context compression."""

import json
import logging

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Use the same conservative character estimate as AstrBot's fallback counter."""
    text = str(text or "")
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    return max(1, int(chinese * 0.6 + (len(text) - chinese) * 0.3))


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
    """Apply Codex/AstrBot-style summary compression to one group context."""

    def __init__(self, config, db, agent, summary_prompt: str):
        self.config = config
        self.db = db
        self.agent = agent
        self.summary_prompt = summary_prompt

    def should_compress(self, rows: list[dict], system_prompt: str, user_prompt: str, tools: list) -> bool:
        used = estimate_tokens(system_prompt) + estimate_tokens(user_prompt)
        used += sum(estimate_tokens(json.dumps(tool.to_openai_tool(), ensure_ascii=False)) for tool in tools)
        return used >= self.config.CONTEXT_MAX_TOKENS * self.config.CONTEXT_SUMMARY_TRIGGER_RATIO

    def split_for_summary(self, rows: list[dict]) -> tuple[list[dict], list[dict]]:
        if len(rows) < 2:
            return [], rows
        total = sum(message_tokens(row) for row in rows)
        recent_budget = max(1, int(total * self.config.CONTEXT_SUMMARY_KEEP_RECENT_RATIO))
        used = 0
        split_at = len(rows)
        for index in range(len(rows) - 1, -1, -1):
            row_tokens = message_tokens(rows[index])
            if used and used + row_tokens > recent_budget:
                break
            used += row_tokens
            split_at = index
        if split_at <= 0:
            split_at = 1
        return rows[:split_at], rows[split_at:]

    async def compress_if_needed(
        self,
        *,
        group_id: int,
        rows: list[dict],
        existing_summary: dict | None,
        system_prompt: str,
        user_prompt: str,
        tools: list,
    ) -> tuple[dict | None, list[dict]]:
        if not self.should_compress(rows, system_prompt, user_prompt, tools):
            return existing_summary, rows

        old_rows, recent_rows = self.split_for_summary(rows)
        if not old_rows:
            return existing_summary, rows

        old_text = self._format_rows(old_rows)
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
            summary_text = await self.agent.summarize(
                system_prompt=self.summary_prompt,
                user_prompt=summary_prompt,
                max_tokens=self.config.CONTEXT_SUMMARY_MAX_TOKENS,
            )
            if not summary_text.strip():
                raise RuntimeError("summary model returned empty content")
        except Exception:
            logger.exception("Context summary failed for group %s; keeping original context", group_id)
            self.db.mark_context_summary_status(group_id, "failed")
            return existing_summary, rows

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
