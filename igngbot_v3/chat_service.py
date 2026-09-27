import asyncio
import json
import logging
import random
import re
import os
import io
import base64
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from PIL import Image

from .call_log_db import insert_call_log, mirror_call_to_site
from .context_manager import ContextManager
from .context_manager import format_context_message
from .llm_client import LLMClient
from .onebot_api import OUTBOUND_SOURCE_AI, send_group_text
from .system_prompt_store import SystemPromptStore

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
    def __init__(self, config, db, system_prompt_store=None, storage=None):
        self.config = config
        self.db = db
        # Optional: when present, stored media paths are re-anchored onto the
        # current attachment root before being read back for multimodal calls.
        self.storage = storage
        self.system_prompt_store = system_prompt_store or SystemPromptStore(config, db)
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

    async def _send_group_text(self, group_id, text):
        return await send_group_text(
            self.config,
            group_id,
            text,
            db=self.db,
            message_source=OUTBOUND_SOURCE_AI,
        )

    def _read_prompt(self, filename: str) -> str:
        path = Path(self.config.PROMPT_DIR) / filename
        return path.read_text(encoding="utf-8") if path.exists() else ""

    async def maybe_reply(self, ctx: MessageContext, history_start_msg_id: str | None = None) -> dict:
        # A recall can arrive while a debounced chat worker is waiting. Check the
        # persisted row before reading or sending its original body to the LLM.
        if self._is_recalled_message(ctx):
            logger.info("Skipping recalled message before chat analysis: group=%s msg=%s", ctx.group_id, ctx.msg_id)
            return {"should_reply": False, "reason": "recalled"}

        if not self.system_prompt_store.get("chat"):
            logger.error("Skipping chat request because no valid local system prompt is loaded")
            return {"should_reply": False, "reason": "system_prompt_unavailable"}

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

        system_prompt = self._build_system_prompt(ctx, group_config)
        user_prompt = self._build_user_prompt(ctx, history_text, history, summary)
        summary, history = await self.context_manager.compress_if_needed(
            group_id=ctx.group_id,
            rows=history,
            existing_summary=summary,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if self._is_recalled_message(ctx):
            logger.info("Skipping recalled message before LLM request: group=%s msg=%s", ctx.group_id, ctx.msg_id)
            return {"should_reply": False, "reason": "recalled"}
        history_text = self._format_history(history)
        user_prompt = self._build_user_prompt(ctx, history_text, history, summary)

        # Build final user content (multimodal list if images available and enabled, otherwise plain text)
        user_content = self._build_user_content(ctx, user_prompt, history)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        model_name = getattr(self.config, "LLM_LOCAL_MODEL", self.config.OPENAI_CHAT_MODEL)
        base_url = getattr(self.config, "LLM_LOCAL_BASE_URL", self.config.OPENAI_BASE_URL)
        api_key = getattr(self.config, "LLM_LOCAL_API_KEY", self.config.OPENAI_API_KEY)

        request_started = time.perf_counter()
        request_succeeded = False
        error_message = ""
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
            request_succeeded = bool(raw_text)
            if not raw_text:
                error_message = "LLM 返回空响应"
        except Exception as exc:
            logger.exception("LLM chat request failed: %s", exc)
            raw_text = ""
            token_usage = None
            error_message = str(exc).strip()[:2000] or exc.__class__.__name__

        duration_ms = max(0, int(round((time.perf_counter() - request_started) * 1000)))
        decision = self._parse_decision(raw_text)
        if decision is None:
            request_succeeded = False
            if not error_message:
                error_message = "模型返回内容无法解析为有效聊天决策"

        user_prompt_for_log = self._prompt_for_log(user_content)
        call_log_id = await insert_call_log(
            group_id=str(ctx.group_id),
            sender_id=str(ctx.sender_id),
            sender_name=ctx.sender_name,
            message_text=ctx.rich_text[:2000],
            call_type="chat",
            model=model_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt_for_log,
            response_content=raw_text[:65535],
            tool_calls=None,
            token_usage=token_usage,
            duration_ms=duration_ms,
            success=request_succeeded,
            error_message=error_message,
        )
        await mirror_call_to_site(
            call_log_id=call_log_id,
            group_id=str(ctx.group_id),
            sender_id=str(ctx.sender_id),
            sender_name=ctx.sender_name,
            message_text=ctx.rich_text[:2000],
            call_type="chat",
            model=model_name,
            system_prompt=system_prompt,
            user_prompt=user_prompt_for_log,
            response_content=raw_text[:65535],
            token_usage=token_usage,
            duration_ms=duration_ms,
            success=request_succeeded,
            error_message=error_message,
        )

        if decision is None:
            logger.warning("Failed to parse final decision, suppressing reply. raw=%s", raw_text)
            return {"should_reply": False, "reply_text": ""}

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

    def _build_system_prompt(self, ctx: MessageContext, group_config: dict) -> str:
        base_prompt = self.system_prompt_store.get("chat").strip()
        if not base_prompt:
            raise RuntimeError("No valid local chat system prompt is loaded")

        dynamic_context = [
            "## 当前状态",
            f"- 当前群聊天模式: {'开启' if group_config.get('is_chat_mode') else '关闭'}。",
            "- 当前消息已经写入数据库；user prompt 中的最后一条记录是本次触发消息。",
            "- 你是纯文本群聊机器人，无法执行外部命令，也无法查询服务器内部或互联网实时数据。遇到不知情的内容明确回答不了解，严禁编造事实。",
        ]
        if ctx.direct_mention:
            dynamic_context.append("- 本条消息显式 @了你或回复了你：符合能力范围时必须回复。")

        prompt_metadata = self.system_prompt_store.metadata()
        logger.debug(
            "Building chat system prompt from local cache: key=chat sha256=%s source=%s",
            prompt_metadata.get("sha256", ""),
            prompt_metadata.get("source", "local-cache"),
        )
        return "\n\n".join((base_prompt, "\n".join(dynamic_context))).strip()

    def _build_user_prompt(
        self,
        ctx: MessageContext,
        history_text: str,
        history: list[dict] | None = None,
        summary: dict | None = None,
    ) -> str:
        summary_text = (summary or {}).get("summary_text") or "无"
        current_text = ctx.rich_text
        for row in reversed(history or []):
            if str(row.get("msg_id")) == str(ctx.msg_id):
                current_text = format_context_message(row, include_attachment_marker=False)
                break
        text = (
            f"[当前群号] {ctx.group_id}\n"
            f"[当前发送者] {ctx.sender_name} (QQ: {ctx.sender_id})\n"
            f"[是否显式@你] {'是' if ctx.direct_mention else '否'}\n"
            f"[本次最新消息ID] {ctx.msg_id}\n"
            f"[本次最新消息正文] {current_text}\n"
            "[此前群聊上下文摘要]\n"
            f"{summary_text}\n"
            "[摘要之后的完整群聊记录]\n"
            f"{history_text}\n\n"
            "注意：记录按时间升序排列，最后一条就是本次最新消息。请优先回应最新消息；摘要只用于补充背景，不要把摘要中的旧话题误当成当前问题。"
        )
        return text

    def _load_image_as_data_url(self, file_path: str, max_dimension: int = 1280) -> str | None:
        """Read a local image file and encode it as a base64 data URL with webp/jpeg compression."""
        if not file_path or not os.path.isfile(file_path):
            return None
        try:
            with Image.open(file_path) as img:
                width, height = img.size
                if max(width, height) > max_dimension:
                    ratio = max_dimension / max(width, height)
                    new_size = (int(width * ratio), int(height * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="WEBP", quality=80)
                encoded = base64.b64encode(buf.getvalue()).decode("ascii")
                return f"data:image/webp;base64,{encoded}"
        except Exception as exc:
            logger.warning("Failed to encode image to data URL (%s): %s", file_path, exc)
            return None

    def _resolve_media_path(self, path) -> str:
        """Re-anchor a persisted media path onto the current attachment root."""
        raw = str(path or "").strip()
        if not raw:
            return ""
        if os.path.isfile(raw):
            return raw
        storage = getattr(self, "storage", None)
        if storage is not None:
            resolved = storage.resolve_path(raw)
            if resolved:
                return resolved
        return raw

    def _build_user_content(
        self,
        ctx: MessageContext,
        text_prompt: str,
        history: list[dict] | None = None,
        max_images: int = 3,
    ) -> str | list[dict]:
        """Build OpenAI multimodal content list if LLM_LOCAL_MULTIMODAL is enabled and images exist."""
        if not getattr(self.config, "LLM_LOCAL_MULTIMODAL", False):
            return text_prompt

        image_items: list[tuple[str, str]] = []  # [(label/msg_id, path)]
        seen_paths = set()

        # 1. Inspect current message files first. A recall may arrive after the
        # message was persisted but before the debounced worker reaches this
        # method, so consult the database again instead of trusting ctx.files.
        if not self._is_recalled_message(ctx):
            for f in getattr(ctx, "files", []) or []:
                if f.get("type") in ("image", "mface"):
                    p = self._resolve_media_path(f.get("stored_path"))
                    if p and p not in seen_paths and os.path.isfile(p):
                        seen_paths.add(p)
                        image_items.append((f"最新消息 [{ctx.msg_id}]", p))

        # 2. Inspect recent messages in history (from newest to older)
        for row in reversed(history or []):
            if len(image_items) >= max_images:
                break
            if row.get("is_recalled"):
                continue
            attachments = row.get("attachments_json")
            if isinstance(attachments, str):
                try:
                    attachments = json.loads(attachments)
                except Exception:
                    attachments = []
            for att in attachments or []:
                if len(image_items) >= max_images:
                    break
                if isinstance(att, dict) and att.get("type") in ("image", "mface"):
                    p = self._resolve_media_path(att.get("stored_path"))
                    if p and p not in seen_paths and os.path.isfile(p):
                        seen_paths.add(p)
                        row_msg_id = row.get("msg_id", "")
                        image_items.append((f"历史消息 [{row_msg_id}]", p))

        if not image_items:
            return text_prompt

        content: list[dict] = [{"type": "text", "text": text_prompt}]
        for label, path in image_items:
            data_url = self._load_image_as_data_url(path)
            if data_url:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": data_url},
                })

        return content

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

    def _get_stored_message(self, ctx: MessageContext) -> dict | None:
        getter = getattr(getattr(self, "db", None), "get_message_by_msg_id", None)
        if getter is None:
            return None
        row = getter(ctx.group_id, ctx.msg_id)
        return dict(row) if row else None

    def _is_recalled_message(self, ctx: MessageContext) -> bool:
        row = self._get_stored_message(ctx)
        return bool(row and row.get("is_recalled"))

    def _format_history(self, rows: list[dict]) -> str:
        lines = []
        for row in rows:
            sender = "云萤" if row.get("is_self") else str(row.get("sender_id"))
            content = format_context_message(row, include_attachment_marker=False)
            if not content:
                continue
            lines.append(f"[{row.get('msg_id')}] {sender}: {content}")
        return "\n".join(lines)

    def _ensure_current_message_present(self, history: list[dict], ctx: MessageContext) -> list[dict]:
        rows = [row for row in (history or []) if str(row.get("msg_id")) != str(ctx.msg_id)]
        stored = self._get_stored_message(ctx)
        if stored:
            # Prefer the persisted row, including its current recall flag, over
            # the in-memory callback payload. This prevents a late recall from
            # reintroducing ctx.rich_text or attachments into the prompt.
            stored.setdefault("msg_id", str(ctx.msg_id))
            stored.setdefault("sender_id", ctx.sender_id)
            if stored.get("id") is not None and stored.get("_db_id") is None:
                stored["_db_id"] = stored["id"]
            rows.append(stored)
        else:
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
        return {
            "should_reply": should_reply,
            "reply_text": reply_text,
        }

    async def apply_decision(self, ctx: MessageContext, decision: dict) -> None:
        reply_text = (decision.get("reply_text") or "").strip()
        if decision.get("should_reply") and reply_text:
            await self._send_group_text(ctx.group_id, reply_text)
