import asyncio
import json
import logging
import random
import re
import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from .call_log_db import insert_call_log
from .context_manager import ContextManager
from .llm_client import LLMClient
from .onebot_api import send_group_text
from .prompt_rules import RISK_SPEECH_CONSTRAINTS

logger = logging.getLogger(__name__)


@dataclass
class MessageContext:
    group_id: int
    sender_id: int
    sender_name: str
    msg_id: str
    rich_text: str
    direct_mention: bool
    files: list[dict] = field(default_factory=list)
    mentioned_user_ids: set[str] = field(default_factory=set)


class ChatService:
    AFFINITY_CHANGE_MESSAGES = {
        "enabled_increase": "诶，好感度从{old}涨到{new}了，云萤今天表现不错嘛",
        "enabled_decrease": "呜，好感度从{old}掉到{new}了，云萤刚刚是不是哪里惹你不开心了",
        "locked_increase": "诶，好感度涨了一点，云萤有在好好表现哦",
        "locked_decrease": "呜，好感度掉了一点，云萤是不是哪里说错话了",
    }

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.llm_client = LLMClient(config)
        self.context_manager = ContextManager(
            config,
            db,
            self.llm_client,
            self._read_prompt("context_summary.txt") or config.CONTEXT_SUMMARY_PROMPT,
            llm_options={
                "base_url": getattr(config, "LLM_LOCAL_BASE_URL", config.OPENAI_BASE_URL),
                "api_key": getattr(config, "LLM_LOCAL_API_KEY", config.OPENAI_API_KEY),
                "model": getattr(config, "LLM_LOCAL_MODEL", config.OPENAI_CHAT_MODEL),
            },
        )

    def _read_prompt(self, filename: str) -> str:
        path = Path(self.config.PROMPT_DIR) / filename
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def maybe_reply(self, ctx: MessageContext, history_start_msg_id: str | None = None) -> dict:
        self.db.ensure_user_config(ctx.sender_id)
        involved_user_ids = {str(ctx.sender_id), *self.db.extract_referenced_user_ids(ctx.rich_text)}
        user_configs = self.db.get_user_configs_batch(list(involved_user_ids))
        group_config = self.db.get_group_config(ctx.group_id) or {}
        summary = self.db.get_context_summary(ctx.group_id)
        summary_boundary = (summary or {}).get("summarized_through_id", 0)
        history = self.db.get_messages_after_id(ctx.group_id, summary_boundary, limit=5000)
        if history_start_msg_id:
            boundary_history = self.db.get_messages_from_msg_id(
                ctx.group_id, history_start_msg_id, limit=5000
            )
            known_ids = {row.get("_db_id") for row in history}
            history = [row for row in boundary_history if row.get("_db_id") not in known_ids] + history
            history.sort(key=lambda row: row.get("_db_id") or 0)
        history = self._ensure_current_message_present(history, ctx)
        history_text = self._format_history(history)

        system_prompt = self._build_system_prompt(ctx, user_configs, group_config)
        user_prompt = self._build_user_prompt(ctx, history_text, history, summary)
        summary, history = await self.context_manager.compress_if_needed(
            group_id=ctx.group_id,
            rows=history,
            existing_summary=summary,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        history_text = self._format_history(history)
        user_prompt = self._build_user_prompt(ctx, history_text, history, summary)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        model_name = getattr(self.config, "LLM_LOCAL_MODEL", self.config.OPENAI_CHAT_MODEL)
        base_url = getattr(self.config, "LLM_LOCAL_BASE_URL", self.config.OPENAI_BASE_URL)
        api_key = getattr(self.config, "LLM_LOCAL_API_KEY", self.config.OPENAI_API_KEY)

        try:
            response_data = await self.llm_client.chat_completion(
                messages=messages,
                temperature=0.3,
                max_tokens=600,
                model=model_name,
                base_url=base_url,
                api_key=api_key,
                timeout=getattr(self.config, "LOCAL_LLM_TIMEOUT", 120),
            )
            choice = (response_data.get("choices") or [{}])[0]
            raw_text = ((choice.get("message") or {}).get("content") or "").strip()
            token_usage = response_data.get("usage")
        except Exception as exc:
            logger.exception("LLM chat request failed: %s", exc)
            raw_text = ""
            token_usage = None

        await insert_call_log(
            group_id=str(ctx.group_id),
            sender_id=str(ctx.sender_id),
            sender_name=ctx.sender_name,
            message_text=ctx.rich_text[:2000],
            call_type="chat",
            model=model_name,
            system_prompt=system_prompt,
            user_prompt=self._prompt_for_log(user_prompt),
            response_content=raw_text[:65535],
            tool_calls=None,
            token_usage=token_usage,
            success=bool(raw_text),
        )

        decision = self._parse_decision(raw_text)
        if decision is None:
            logger.warning("Failed to parse final decision, suppressing reply. raw=%s", raw_text)
            return {"should_reply": False, "reply_text": "", "affinity_updates": {}}

        logger.info(
            "Chat decision: group=%s msg=%s should_reply=%s reply_len=%s",
            ctx.group_id, ctx.msg_id, decision.get("should_reply"),
            len(decision.get("reply_text") or ""),
        )

        if ctx.direct_mention and not decision.get("should_reply"):
            logger.warning("Forcing a minimal reply for direct mention msg %s", ctx.msg_id)
            decision["should_reply"] = True
            decision["reply_text"] = "你说"
        return decision

    def _build_system_prompt(self, ctx: MessageContext, user_configs: dict, group_config: dict) -> str:
        personality = self.db.get_active_personality(ctx.group_id) or {
            "name": "亲和",
            "prompt_text": "",
        }
        user_info_lines = ["## 当前用户信息"]
        for uid, cfg in user_configs.items():
            parts = []
            affinity_value = cfg.get("affinity_value", 50) if cfg.get("affinity_enabled") else 50
            parts.append(f"好感度: {affinity_value}/100")
            if parts:
                user_info_lines.append(f"[{uid}]: {' | '.join(parts)}")

        dynamic_context = [
            "## 当前状态",
            f"- 当前群聊天模式: {'开启' if group_config.get('is_chat_mode') else '关闭'}。",
            "- 当前消息已经写入数据库；user prompt 中的最后一条记录是本次触发消息。",
            "- 你是纯文本群聊机器人，无法执行外部命令，也无法查询服务器内部或互联网实时数据。遇到不知情的内容明确回答不了解，严禁编造事实。",
        ]
        if ctx.direct_mention:
            dynamic_context.append("- 本条消息显式 @了你或回复了你：符合能力范围时必须回复。")

        base_prompt = self.db.get_system_prompt("chat")
        attachment_section = self._build_user_attachment_section()
        fixed_sections = [
            RISK_SPEECH_CONSTRAINTS,
            "# 角色性格\n"
            f"当前性格：{personality['name']}\n"
            f"{personality.get('prompt_text') or ''}".strip(),
            "\n".join(user_info_lines),
            "\n".join(dynamic_context),
        ]
        parts = [base_prompt]
        if attachment_section:
            parts.append(attachment_section)
        parts.extend(fixed_sections)
        return "\n\n".join(part for part in parts if part).strip()

    def _build_user_attachment_section(self) -> str:
        rows = self.db.get_enabled_chat_system_prompt_attachments()
        if not rows:
            return ""
        lines = [
            "## 用户附加系统提示词",
            "以下条目由用户提交并经审核通过，作为聊天模式的附加回复要求。"
            "请在不违反既有更高优先级约束（角色、安全规则）的前提下遵守。",
            "",
        ]
        for idx, row in enumerate(rows, start=1):
            prompt_text = (row.get("prompt_text") or "").strip()
            lines.append(
                f"{idx}. (ID:{row.get('id')} | 用户QQ:{row.get('owner_qq')}) {prompt_text}"
            )
        return "\n".join(lines).strip()

    def _build_user_prompt(
        self,
        ctx: MessageContext,
        history_text: str,
        history: list[dict] | None = None,
        summary: dict | None = None,
    ):
        summary_text = (summary or {}).get("summary_text") or "无"
        text = (
            f"[当前群号] {ctx.group_id}\n"
            f"[当前发送者] {ctx.sender_name} (QQ: {ctx.sender_id})\n"
            f"[是否显式@你] {'是' if ctx.direct_mention else '否'}\n"
            f"[本次最新消息ID] {ctx.msg_id}\n"
            f"[本次最新消息正文] {ctx.rich_text}\n"
            "[此前群聊上下文摘要]\n"
            f"{summary_text}\n"
            "[摘要之后的完整群聊记录]\n"
            f"{history_text}\n\n"
            "注意：记录按时间升序排列，最后一条就是本次最新消息。请优先回应最新消息；摘要只用于补充背景，不要把摘要中的旧话题误当成当前问题。"
        )
        return text

    def _prompt_for_log(self, prompt) -> str:
        if isinstance(prompt, str):
            return prompt
        parts = []
        for block in prompt:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image_url":
                parts.append("[多模态图片]")
        return "\n".join(parts)

    def _format_history(self, rows: list[dict]) -> str:
        lines = []
        for row in rows:
            sender = "云萤" if row.get("is_self") else str(row.get("sender_id"))
            content = row.get("message_content") or row.get("plain_text_content") or ""
            if not content:
                continue
            lines.append(f"[{row.get('msg_id')}] {sender}: {content}")
        return "\n".join(lines)

    def _ensure_current_message_present(self, history: list[dict], ctx: MessageContext) -> list[dict]:
        rows = list(history or [])
        if rows and str(rows[-1].get("msg_id")) == str(ctx.msg_id):
            return rows

        rows = [row for row in rows if str(row.get("msg_id")) != str(ctx.msg_id)]
        rows.append(
            {
                "msg_id": str(ctx.msg_id),
                "sender_id": ctx.sender_id,
                "message_content": ctx.rich_text,
                "plain_text_content": ctx.rich_text,
                "is_self": False,
            }
        )
        return rows

    @staticmethod
    def _normalize_chat_reply(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"^请分析[：:].*$", "", text, flags=re.MULTILINE).strip()
        return text

    def _parse_decision(self, raw_text: str) -> dict | None:
        if not raw_text:
            return None
        clean = raw_text.strip()
        clean = re.sub(r"<think>.*?(?:</think>|$)", "", clean, flags=re.DOTALL).strip()
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            clean = match.group(0)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            return None

        should_reply = bool(data.get("should_reply"))
        reply_text = str(data.get("reply_text") or "").strip()
        reply_text = self._normalize_chat_reply(reply_text)
        affinity_updates = data.get("affinity_updates") or {}
        sanitized_updates = {}
        for uid, value in affinity_updates.items():
            try:
                score = max(0, min(100, int(value)))
                sanitized_updates[str(uid)] = score
            except Exception:
                continue
        return {
            "should_reply": should_reply,
            "reply_text": reply_text,
            "affinity_updates": sanitized_updates,
        }

    async def apply_decision(self, ctx: MessageContext, decision: dict) -> None:
        affinity_updates = decision.get("affinity_updates") or {}
        for uid, score in affinity_updates.items():
            self.db.update_affinity(uid, score)

        reply_text = (decision.get("reply_text") or "").strip()
        if decision.get("should_reply") and reply_text:
            await send_group_text(self.config, ctx.group_id, reply_text)
