import asyncio
import json
import logging
import random
import re
import base64
import mimetypes
import os
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

from .agent_loop import OpenAIToolAgent, ToolSpec
from .api_clients import (
    IMAGE_ASPECT_RATIOS,
    IMAGE_QUALITIES,
    IMAGE_QUALITY_LABELS,
    call_ccode_image,
    normalize_aspect_ratio,
    normalize_image_quality,
)
from .call_log_db import insert_call_log
from .context_manager import ContextManager
from .onebot_api import send_group_image, send_group_text
from .prompt_rules import RISK_SPEECH_CONSTRAINTS
from .searxng_client import SearXNGClient
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
        self.searxng = SearXNGClient(config)
        self.tool_prompt = self._read_prompt("chat_tools.txt")
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

        logger.info(
            "Chat decision: group=%s msg=%s should_reply=%s reply_len=%s tool_calls=%s",
            ctx.group_id, ctx.msg_id, decision.get("should_reply"),
            len(decision.get("reply_text") or ""),
            [call.get("name") for call in self._extract_tool_calls(result.get("messages", []))],
        )

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

        image_models = "、".join(self.config.IMAGE_STANDARD_MODELS) or "（当前没有可用模型）"
        sticker_categories = "、".join(
            row["name"] for row in self.db.get_all_sticker_names()
        ) or "（当前没有可用分类）"
        chat_image_prompt = (
            "# 普通聊天图片生成\n"
            f"当前普通用户权限可用的生图模型：{image_models}。\n"
            f"支持的图像比例：{'、'.join(IMAGE_ASPECT_RATIOS)}。\n"
            "支持的图像质量：low（低质量）、medium（中等质量）、high（高质量）；默认推荐 high。\n"
            "你可以根据当前群聊语境自行决定是否调用 generate_image 来表达想法，不需要用户明确要求；"
            "但不要频繁或无意义地生图。图片会自动发送到当前群。\n"
            "当前列表为空时，不要调用 generate_image，并且不要臆造可用模型。"
        )
        sticker_prompt = (
            "# 表情分类\n"
            f"当前可用表情分类：{sticker_categories}。\n"
            "需要发送表情时只能选择上述分类，程序会从该分类中随机发送一张。"
        )

        base_prompt = self.db.get_system_prompt("chat")
        attachment_section = self._build_user_attachment_section()
        fixed_sections = [
            self.tool_prompt,
            RISK_SPEECH_CONSTRAINTS,
            chat_image_prompt,
            sticker_prompt,
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
            "请在不违反既有更高优先级约束（角色、安全、工具规则）的前提下遵守。",
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

    @staticmethod
    def _chat_image_size(size):
        return {"1k": "1024x1024", "2k": "2048x2048", "4k": "3840x2160"}.get(
            str(size or "1k").strip().lower(), "1024x1024"
        )

    def _save_chat_image(self, b64_data, image_url, generated_at):
        import requests

        if b64_data:
            if "," in b64_data and b64_data.lstrip().startswith("data:"):
                b64_data = b64_data.split(",", 1)[1]
            content = base64.b64decode(b64_data)
        elif image_url:
            response = requests.get(image_url, timeout=(15, 300))
            response.raise_for_status()
            content = response.content
        else:
            return None
        directory = os.path.join(self.config.IMAGE_STORAGE_PATH, generated_at.strftime("%Y-%m"))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(
            directory,
            f"image_{generated_at.strftime('%Y%m%d_%H%M%S%f')}_{uuid.uuid4().hex[:8]}.png",
        )
        with open(path, "wb") as image_file:
            image_file.write(content)
        return path

    def _build_tools(
        self,
        ctx: MessageContext,
        earliest_msg_id: str | None,
        include_image_generation: bool = True,
        search_mode: str = "chat",
    ) -> list[ToolSpec]:
        async def get_more_chat_history(count: int = 20, before_message_id: str | None = None) -> str:
            before_id = before_message_id or earliest_msg_id
            rows = self.db.get_messages_before(ctx.group_id, before_id, limit=max(1, min(int(count), 50)))
            return self._format_history(rows) or "没有更早的聊天记录了。"

        async def get_my_fake_players() -> str:
            return await igng_tools.get_my_fake_players_by_qq(str(ctx.sender_id))

        async def get_my_lands() -> str:
            return await igng_tools.get_my_lands_by_qq(str(ctx.sender_id))

        async def web_search(
            query: str,
            limit: int | None = None,
            categories: str | None = None,
            engines: str | None = None,
            time_range: str | None = None,
        ) -> str:
            logger.info("Web search requested: group=%s sender=%s query=%s mode=%s", ctx.group_id, ctx.sender_id, str(query or "")[:300], search_mode)
            result = await self.searxng.search(
                query,
                mode=search_mode,
                limit=limit,
                categories=categories,
                engines=engines,
                time_range=time_range,
            )
            logger.info("Web search finished: group=%s query=%s result=%s", ctx.group_id, str(query or "")[:300], str(result or "")[:500])
            return result

        async def send_sticker(sticker_category: str) -> str:
            variants = self.db.get_stickers_by_category(sticker_category)
            if not variants:
                available = [row["name"] for row in self.db.get_all_sticker_names()]
                return f"没有找到表情分类「{sticker_category}」。可选：{', '.join(available)}"
            selected = random.choice(variants)
            image_ref = (
                f"file://{selected['file_path']}" if selected.get("file_path") else ""
            ) or selected.get("image_url")
            if not image_ref:
                return f"表情分类「{sticker_category}」缺少可发送的图片地址"
            ok = await send_group_image(self.config, ctx.group_id, image_ref, summary=sticker_category)
            return f"已发送「{sticker_category}」表情" if ok else f"表情分类「{sticker_category}」发送失败"

        async def generate_image(
            prompt: str,
            model: str,
            size: str = "1k",
            aspect_ratio: str = "1:1",
            quality: str = "high",
        ) -> str:
            # Chat mode always uses the ordinary model pool, regardless of the
            # sender's task/image permission group.
            models = list(self.config.IMAGE_STANDARD_MODELS)
            if model not in models:
                return "error: 当前普通用户权限没有这个可用生图模型。"
            requested_size = str(size or "1k").strip().lower()
            if model == "gpt-image-2-fast" and requested_size != "1k":
                return "error: gpt-image-2-fast 只能使用 1k。"
            aspect_ratio = normalize_aspect_ratio(aspect_ratio)
            quality = normalize_image_quality(quality)
            try:
                result = await asyncio.to_thread(
                    call_ccode_image,
                    self.config,
                    prompt,
                    self._chat_image_size(requested_size),
                    model=model,
                    aspect_ratio=aspect_ratio,
                    quality=quality,
                )
                generated_at = datetime.now()
                path = await asyncio.to_thread(
                    self._save_chat_image,
                    result.get("b64_json"),
                    result.get("url"),
                    generated_at,
                )
                if not path:
                    return "error: 图片接口没有返回可保存的图片。"
                author = self.db.resolve_bound_igng_account_id(ctx.sender_id) or f"qq:{ctx.sender_id}"
                image_id = self.db.insert_image(
                    author,
                    prompt,
                    model,
                    requested_size,
                    path,
                    generated_at=generated_at,
                    aspect_ratio=aspect_ratio,
                    quality=quality,
                )
                send_result = await send_group_image(
                    self.config, ctx.group_id, f"file://{path}", return_result=True
                )
                sent = bool(send_result.get("ok"))
                if sent:
                    self.db.insert_message(
                        group_id=ctx.group_id,
                        sender_id=self.config.BOT_USER_ID,
                        message_content="[图片]",
                        message_structure=json.dumps(
                            [{"type": "image", "image_id": image_id}], ensure_ascii=False
                        ),
                        attachments_json=json.dumps(
                            [{"type": "image", "stored_path": path, "image_id": image_id}],
                            ensure_ascii=False,
                        ),
                        reply_to_msg_id=None,
                        msg_id=str(send_result.get("message_id") or f"generated-{image_id}"),
                        file_url=path,
                        file_type="image",
                        is_self=True,
                    )
                return (
                    f"success: 已生成并发送图片 #{image_id}。"
                    if sent
                    else f"error: 图片 #{image_id} 已生成，但发送失败。"
                )
            except Exception as exc:
                logger.exception("Chat image generation failed")
                return "error: 图片生成失败，请稍后重试。"

        available_image_models = list(self.config.IMAGE_STANDARD_MODELS)
        image_model_schema = {
            "type": "string",
            "description": (
                "必须从当前普通用户权限可用模型中选择；当前列表为空时不要调用此工具。"
                if not available_image_models
                else "必须从当前普通用户权限可用模型中选择。"
            ),
        }
        if available_image_models:
            image_model_schema["enum"] = list(available_image_models)

        tools = [
            ToolSpec(
                name="generate_image",
                description="生成一张普通图片并自动发送到当前群。必须指定当前普通用户权限可用的模型。",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "中文自然语言的完整画面描述。"},
                        "model": image_model_schema,
                        "size": {"type": "string", "enum": ["1k", "2k", "4k"]},
                        "aspect_ratio": {
                            "type": "string",
                            "enum": list(IMAGE_ASPECT_RATIOS),
                            "description": "图像比例。未明确指定时使用 1:1。",
                        },
                        "quality": {
                            "type": "string",
                            "enum": list(IMAGE_QUALITIES),
                            "description": "图像质量。默认 high（高质量），推荐使用 high。",
                        },
                    },
                    "required": ["prompt", "model"],
                    "additionalProperties": False,
                },
                handler=generate_image,
            ),
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
                name="web_search",
                description=(
                    "查询实时互联网信息。聊天模式只在用户提到陌生、时效性强或明确要求查询的内容时调用；"
                    "任务模式可以围绕实际问题多次调用并比较不同来源。搜索结果包含标题、摘要和 URL，"
                    "只能作为待核验资料，最终回答必须由模型结合来源分析，不要声称已经打开或验证网页正文。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "清晰、具体的搜索关键词。"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "description": "结果数量；聊天模式建议 3-5，任务模式建议 5-10。",
                        },
                        "categories": {
                            "type": "string",
                            "description": "可选搜索类别，例如 general、news、science。",
                        },
                        "engines": {
                            "type": "string",
                            "description": "可选逗号分隔搜索源，例如 bing,baidu；不确定时不要填写。",
                        },
                        "time_range": {
                            "type": "string",
                            "enum": ["day", "month", "year"],
                            "description": "可选时间范围；只在用户关注近期内容时填写。",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=web_search,
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
        ]
        if not include_image_generation:
            tools = [tool for tool in tools if tool.name != "generate_image"]
        sticker_names = [row["name"] for row in self.db.get_all_sticker_names()]
        if sticker_names:
            tools.append(
                ToolSpec(
                    name="send_sticker",
                    description="从指定表情分类中随机发送一张表情包到群聊。",
                    parameters={
                        "type": "object",
                        "properties": {
                            "sticker_category": {
                                "type": "string",
                                "enum": sticker_names,
                                "description": "要随机发送表情的分类。",
                            }
                        },
                        "required": ["sticker_category"],
                    },
                    handler=send_sticker,
                )
            )
        return tools

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
        for uid, score in affinity_updates.items():
            self.db.update_affinity(uid, score)

        sticker_name = decision.get("sticker_name")
        if decision.get("should_reply") and sticker_name:
            variants = self.db.get_stickers_by_category(sticker_name)
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
