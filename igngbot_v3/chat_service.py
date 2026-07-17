import json
import logging
import random
import re
import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path

from .agent_loop import OpenAIToolAgent, ToolSpec
from .call_log_db import insert_call_log
from .context_manager import ContextManager
from .onebot_api import send_group_image, send_group_text
from .tools import igng_tools

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
        self.agent = OpenAIToolAgent(config)
        self.system_prompt = self._read_prompt("system.txt")
        self.context_manager = ContextManager(
            config,
            db,
            self.agent,
            self._read_prompt("context_summary.txt") or config.CONTEXT_SUMMARY_PROMPT,
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
        earliest_msg_id = history[0]["msg_id"] if history else None

        tool_specs = self._build_tools(ctx, earliest_msg_id)
        system_prompt = self._build_system_prompt(ctx, user_configs, group_config)
        user_prompt = self._build_user_prompt(ctx, history_text, history, summary)
        summary, history = await self.context_manager.compress_if_needed(
            group_id=ctx.group_id,
            rows=history,
            existing_summary=summary,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tool_specs,
        )
        history_text = self._format_history(history)
        earliest_msg_id = history[0]["msg_id"] if history else None
        tool_specs = self._build_tools(ctx, earliest_msg_id)
        user_prompt = self._build_user_prompt(ctx, history_text, history, summary)

        async def send_tool_progress(text: str):
            text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.DOTALL).strip()
            if text:
                await send_group_text(self.config, ctx.group_id, text)

        result = await self.agent.run(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tool_specs,
            on_tool_progress=send_tool_progress,
        )
        raw_text = (result.get("text") or "").strip()

        await insert_call_log(
            group_id=str(ctx.group_id),
            sender_id=str(ctx.sender_id),
            sender_name=ctx.sender_name,
            message_text=ctx.rich_text[:2000],
            call_type="agent",
            model=self.config.CLOUD_LLM_MODEL,
            system_prompt=system_prompt,
            user_prompt=self._prompt_for_log(user_prompt),
            response_content=raw_text[:65535],
            tool_calls=json.dumps(
                self._extract_tool_calls(result.get("messages", [])),
                ensure_ascii=False,
            ),
            token_usage=result.get("token_usage"),
            success=True,
        )

        decision = self._parse_decision(raw_text)
        if decision is None:
            logger.warning("Failed to parse final decision, suppressing reply. raw=%s", raw_text)
            return {"should_reply": False, "reply_text": "", "sticker_name": None, "affinity_updates": {}}

        if ctx.direct_mention and not decision.get("should_reply"):
            tool_calls = self._extract_tool_calls(result.get("messages", []))
            used_sticker = any(call.get("name") == "send_sticker" for call in tool_calls)
            if not used_sticker:
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
            # 锁定只影响传给 AI 的快照，不影响数据库中的真实好感度。
            affinity_value = cfg.get("affinity_value", 50) if cfg.get("affinity_enabled") else 50
            parts.append(f"好感度: {affinity_value}/100")
            if parts:
                user_info_lines.append(f"[{uid}]: {' | '.join(parts)}")

        dynamic_context = [
            "## 当前状态",
            f"- 当前群聊天模式: {'开启' if group_config.get('is_chat_mode') else '关闭'}。",
            "- 当前消息已经写入数据库；user prompt 中的最后一条记录是本次触发消息。",
        ]
        if ctx.direct_mention:
            dynamic_context.append("- 本条消息显式 @了你或回复了你：符合能力范围时必须回复。")

        return "\n\n".join(
            [
                self.system_prompt,
                "# 角色性格\n"
                f"当前性格：{personality['name']}\n"
                f"{personality.get('prompt_text') or ''}".strip(),
                "\n".join(user_info_lines),
                "\n".join(dynamic_context),
            ]
        ).strip()

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
        image_blocks = []
        image_sources = []
        for file_info in ctx.files or []:
            image_sources.append((f"本次消息 {ctx.msg_id}", file_info))
        for row in reversed(history or []):
            attachments = row.get("attachments_json")
            if attachments:
                try:
                    attachments = json.loads(attachments) if isinstance(attachments, str) else attachments
                except json.JSONDecodeError:
                    attachments = []
                for file_info in attachments or []:
                    image_sources.append((f"历史消息 {row.get('msg_id')}", file_info))
            elif row.get("file_type") == "image" and row.get("file_url"):
                image_sources.append(
                    (
                        f"历史消息 {row.get('msg_id')}",
                        {"type": "image", "url": row.get("file_url")},
                    )
                )

        for label, file_info in image_sources[:6]:
            if file_info.get("type") != "image":
                continue
            image_blocks.append({"type": "text", "text": f"[{label}的图片]"})
            local_path = file_info.get("stored_path")
            if local_path:
                try:
                    with open(local_path, "rb") as image_file:
                        encoded = base64.b64encode(image_file.read()).decode("ascii")
                    mime = mimetypes.guess_type(local_path)[0] or "image/png"
                    image_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        }
                    )
                    continue
                except OSError:
                    logger.warning("Failed to load local image for multimodal prompt: %s", local_path)
            image_url = file_info.get("url")
            if image_url:
                image_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    }
                )
        if not image_blocks:
            return text
        return [{"type": "text", "text": text}, *image_blocks]

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

    def _build_tools(self, ctx: MessageContext, earliest_msg_id: str | None) -> list[ToolSpec]:
        async def get_more_chat_history(count: int = 20, before_message_id: str | None = None) -> str:
            before_id = before_message_id or earliest_msg_id
            rows = self.db.get_messages_before(ctx.group_id, before_id, limit=max(1, min(int(count), 50)))
            return self._format_history(rows) or "没有更早的聊天记录了。"

        async def get_my_fake_players() -> str:
            return await igng_tools.get_my_fake_players_by_qq(str(ctx.sender_id))

        async def get_my_lands() -> str:
            return await igng_tools.get_my_lands_by_qq(str(ctx.sender_id))

        async def send_sticker(sticker_name: str) -> str:
            variants = self.db.get_stickers_by_name(sticker_name)
            if not variants:
                available = [row["name"] for row in self.db.get_all_sticker_names()]
                return f"没有名为「{sticker_name}」的表情包。可选：{', '.join(available)}"
            selected = random.choice(variants)
            image_ref = (
                f"file://{selected['file_path']}" if selected.get("file_path") else ""
            ) or selected.get("image_url")
            if not image_ref:
                return f"表情「{sticker_name}」缺少可发送的图片地址"
            ok = await send_group_image(self.config, ctx.group_id, image_ref, summary=sticker_name)
            return f"已发送「{sticker_name}」表情" if ok else f"表情「{sticker_name}」发送失败"

        return [
            ToolSpec(
                name="get_more_chat_history",
                description="获取更早的群聊记录，用于理解上下文和决定是否回复。",
                parameters={
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "description": "想再获取多少条更早的消息，1到50。"},
                        "before_message_id": {"type": "string", "description": "从哪条消息之前继续向前获取；不填则从当前已提供历史的最早一条继续。"},
                    },
                },
                handler=get_more_chat_history,
            ),
            ToolSpec(
                name="get_server_performance",
                description="获取 MC 服务器性能数据（TPS、MSPT、CPU、内存、在线玩家数）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string", "description": "服务器名或别名。"},
                        "range_hours": {"type": "integer", "description": "查询范围小时数，默认24，最大336。"},
                    },
                    "required": ["server_name"],
                },
                handler=igng_tools.get_server_performance,
            ),
            ToolSpec(
                name="get_server_latency",
                description="获取 MC 服务器各节点网络延迟。",
                parameters={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string", "description": "服务器名或别名。"},
                        "range_hours": {"type": "integer", "description": "查询范围小时数，默认24，最大336。"},
                    },
                    "required": ["server_name"],
                },
                handler=igng_tools.get_server_latency,
            ),
            ToolSpec(
                name="get_fake_players",
                description="获取指定 MC 服务器上的假人信息。",
                parameters={
                    "type": "object",
                    "properties": {"server_name": {"type": "string", "description": "服务器名或别名。"}},
                    "required": ["server_name"],
                },
                handler=igng_tools.get_fake_players,
            ),
            ToolSpec(
                name="query_player_punishments",
                description="查询玩家处罚记录。",
                parameters={
                    "type": "object",
                    "properties": {"player_names": {"type": "string", "description": "一个或多个玩家名。"}},
                    "required": ["player_names"],
                },
                handler=igng_tools.query_player_punishments,
            ),
            ToolSpec(
                name="get_my_fake_players",
                description="查询当前发言人名下全部假人。",
                parameters={"type": "object", "properties": {}},
                handler=get_my_fake_players,
            ),
            ToolSpec(
                name="get_my_lands",
                description="查询当前发言人名下全部领地。",
                parameters={"type": "object", "properties": {}},
                handler=get_my_lands,
            ),
            ToolSpec(
                name="get_public_lands",
                description="查询某玩家的公开领地。",
                parameters={
                    "type": "object",
                    "properties": {"player_name": {"type": "string", "description": "玩家游戏名。"}},
                    "required": ["player_name"],
                },
                handler=igng_tools.get_public_lands,
            ),
            ToolSpec(
                name="query_igng_user_info",
                description="查询 IGNG 网站用户信息。",
                parameters={
                    "type": "object",
                    "properties": {"qq_or_username": {"type": "string", "description": "QQ号或用户名。"}},
                    "required": ["qq_or_username"],
                },
                handler=igng_tools.query_igng_user_info,
            ),
            ToolSpec(
                name="get_post_detail",
                description="查询 IGNG 网站文章详情。",
                parameters={
                    "type": "object",
                    "properties": {"post_id": {"type": "integer", "description": "文章 ID。"}},
                    "required": ["post_id"],
                },
                handler=igng_tools.get_post_detail,
            ),
            ToolSpec(
                name="send_sticker",
                description="发送表情包到群聊。",
                parameters={
                    "type": "object",
                    "properties": {"sticker_name": {"type": "string", "description": "表情名。"}},
                    "required": ["sticker_name"],
                },
                handler=send_sticker,
            ),
        ]

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

    def _extract_tool_calls(self, messages: list[dict]) -> list[dict]:
        tool_calls = []
        for message in messages:
            if message.get("role") == "assistant":
                for tool_call in message.get("tool_calls") or []:
                    function_info = tool_call.get("function") or {}
                    tool_calls.append(
                        {
                            "name": function_info.get("name"),
                            "args": function_info.get("arguments"),
                        }
                    )
        return tool_calls

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
        sticker_name = data.get("sticker_name")
        if sticker_name is not None:
            sticker_name = str(sticker_name).strip() or None
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
            "sticker_name": sticker_name,
            "affinity_updates": sanitized_updates,
        }

    async def apply_decision(self, ctx: MessageContext, decision: dict) -> None:
        affinity_updates = decision.get("affinity_updates") or {}
        previous_configs = self.db.get_user_configs_batch(list(affinity_updates))
        affinity_notices = []
        for uid, score in affinity_updates.items():
            previous = previous_configs.get(str(uid), {})
            old_score = int(previous.get("affinity_value", 50))
            new_score = int(score)
            self.db.update_affinity(uid, score)
            if new_score == old_score:
                continue
            direction = "increase" if new_score > old_score else "decrease"
            mode = "enabled" if previous.get("affinity_enabled") else "locked"
            template = self.AFFINITY_CHANGE_MESSAGES[f"{mode}_{direction}"]
            affinity_notices.append(template.format(old=old_score, new=new_score))

        sticker_name = decision.get("sticker_name")
        if decision.get("should_reply") and sticker_name:
            variants = self.db.get_stickers_by_name(sticker_name)
            if variants:
                selected = random.choice(variants)
                image_ref = (
                    f"file://{selected['file_path']}" if selected.get("file_path") else ""
                ) or selected.get("image_url")
                if image_ref:
                    await send_group_image(self.config, ctx.group_id, image_ref, summary=sticker_name)

        reply_text = (decision.get("reply_text") or "").strip()
        if decision.get("should_reply") and reply_text:
            await send_group_text(self.config, ctx.group_id, reply_text)
        for notice in affinity_notices:
            await send_group_text(self.config, ctx.group_id, notice)
