import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import Future
from logging.handlers import RotatingFileHandler

from .call_log_db import close_call_log_pool, ensure_call_logs_table
from .chat_service import ChatService, MessageContext
from .config import Config
from .db import DBHandler
from .message_parser import apply_forward_content, parse_message
from .message_ingest import persist_message
from .mc_ticket_notifications import McTicketNotifier
from .onebot_client import OneBotClient
from .onebot_api import (
    OUTBOUND_SOURCE_AUTO_PLUS_ONE,
    OUTBOUND_SOURCE_COMMAND,
    get_forward_msg,
    send_group_text,
)
from .storage import StorageHandler
from .system_prompt_store import SystemPromptStore
from .media_text import MediaTextExtractor, MediaTextResult, append_media_text
from .message_media import hydrate_structure_media
from .timeutil import unix_to_utc_naive
from .user_config_db import close_pool as close_user_config_pool
from .user_config_db import ensure_group_configs_table


def _build_log_handlers():
    handlers = [logging.StreamHandler(sys.stdout)]
    log_dir = os.getenv("LOCAL_STORAGE", Config.LOCAL_STORAGE)
    os.makedirs(log_dir, exist_ok=True)
    handlers.append(
        RotatingFileHandler(
            os.path.join(log_dir, "igngbot_v3.log"),
            maxBytes=int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024))),
            backupCount=int(os.getenv("LOG_BACKUP_COUNT", "5")),
            encoding="utf-8",
        )
    )
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_build_log_handlers(),
)
logger = logging.getLogger("igngbot_v3")


class App:
    _HELP_CATEGORIES = {
        "聊天": "群聊模式和自然对话",
        "管理": "通知和群聊总结等管理员功能",
    }

    _HELP_DETAILS = {
        "聊天": (
            "聊天帮助\n"
            "聊天模式使用本地模型；在群聊中负责自然对话。\n"
            "/聊天模式\n切换当前群聊天模式，仅群主、群管理员或 bot 管理员可用。"
        ),
        "管理": (
            "管理帮助\n"
            "/通知 <内容>\n向默认通知群发送通知。\n"
            "/通知 <内容> <群号,群号>\n向指定群发送通知。\n"
            "/通知 <内容> 全员\n发送并 @全员。\n"
            "/总结 <时间> [群号,群号]\n总结指定时间范围的群聊。\n"
            "/用户组\n查询当前用户组。\n"
            "/用户组 <pro|plus> <IGNG用户ID>\n设置用户组，仅 bot 管理员可用。\n"
            "管理类指令需要 bot 管理员权限，部分操作还需要群管理员权限。"
        ),
    }

    def __init__(self):
        self.config = Config()
        self.db = DBHandler(self.config)
        self.system_prompt_store = SystemPromptStore(self.config, self.db)
        # Storage is constructed first so ChatService can re-anchor persisted
        # media paths onto the current attachment root when building multimodal
        # prompts (rows written on the old host carry a different prefix).
        self.storage = StorageHandler(self.config)
        self.chat_service = ChatService(
            self.config,
            self.db,
            system_prompt_store=self.system_prompt_store,
            storage=self.storage,
        )
        self.media_text = MediaTextExtractor(self.config)
        self.mc_ticket_notifier = McTicketNotifier(self.config, db=self.db)
        self._chat_workers = {}
        self._chat_pending = {}
        self._chat_pending_boundaries = {}
        self._chat_last_reply_at = {}
        self._plus_one_states = {}
        self._plus_one_queued_decisions = {}
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(
            target=self._run_loop,
            name="igngbot-v3-loop",
            daemon=True,
        )

    async def _send_group_text(
        self,
        _config,
        group_id,
        text,
        *,
        message_source=OUTBOUND_SOURCE_COMMAND,
    ):
        """Send a command/automatic reply and persist it in message_logs."""
        return await send_group_text(
            self.config,
            group_id,
            text,
            db=self.db,
            message_source=message_source,
        )

    def startup(self):
        self.db.connect()
        self.db.init_table()
        self.db.init_group_configs_table()
        self.storage.check_available()
        self.loop_thread.start()
        self._run_coro_sync(self._startup_async()).result()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run_coro_sync(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _startup_async(self):
        await self.system_prompt_store.initialize()
        self.system_prompt_store.start()
        await ensure_group_configs_table()
        await ensure_call_logs_table()
        await close_user_config_pool()
        await close_call_log_pool()
        await self.mc_ticket_notifier.start()

    def handle_raw_message(self, data: dict):
        future = self._run_coro_sync(self._handle_raw_message_async(data))
        future.add_done_callback(self._log_future_error)

    def handle_recall_event(self, data: dict):
        """Schedule a OneBot group-recall notice without entering message handling."""
        future = self._run_coro_sync(self._handle_recall_event_async(data))
        future.add_done_callback(self._log_recall_future_error)

    def _log_future_error(self, future: Future):
        try:
            future.result()
        except Exception:
            logger.exception("Message processing failed")

    def _log_recall_future_error(self, future: Future):
        try:
            future.result()
        except Exception:
            logger.exception("Recall event processing failed")

    async def _handle_recall_event_async(self, data: dict):
        if not isinstance(data, dict):
            logger.warning("Ignoring malformed group_recall event: expected object, got %r", type(data))
            return None

        raw_group_id = data.get("group_id")
        raw_message_id = data.get("message_id")
        if raw_group_id in (None, "") or raw_message_id in (None, ""):
            logger.warning(
                "Ignoring malformed group_recall event: group_id=%r message_id=%r",
                raw_group_id,
                raw_message_id,
            )
            return None
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring malformed group_recall event: invalid group_id=%r", raw_group_id)
            return None

        message_id = str(raw_message_id).strip()
        if not message_id:
            logger.warning("Ignoring malformed group_recall event: empty message_id")
            return None

        raw_operator_id = data.get("operator_id")
        operator_id = None
        if raw_operator_id not in (None, ""):
            try:
                operator_id = int(raw_operator_id)
            except (TypeError, ValueError):
                # operator_id is auxiliary metadata; do not drop an otherwise
                # valid recall just because the client sent it malformed.
                logger.warning(
                    "Ignoring invalid group_recall operator_id=%r for group=%s msg=%s",
                    raw_operator_id,
                    group_id,
                    message_id,
                )

        result = self.db.mark_message_recalled(
            group_id=group_id,
            msg_id=message_id,
            recall_operator_id=operator_id,
            recalled_at=unix_to_utc_naive(data.get("time")),
        )
        logger.info(
            "Recall event applied: group=%s msg=%s status=%s",
            group_id,
            message_id,
            result.get("status") if isinstance(result, dict) else result,
        )
        return result

    async def _handle_raw_message_async(self, data: dict):
        parsed = await persist_message(self, data)
        if parsed is None:
            return
        content = parsed.get("message_content", "").strip()
        normalized_content = self._normalize_command_text(content)

        # Private messages are persisted for audit/history, but the current bot
        # command/chat flow is group-scoped. Private outbound messages sent by
        # the notifier are recorded directly by onebot_api.py.
        if parsed.get("conversation_type") != "group":
            return

        if self._is_disabled_mc_ticket_command(normalized_content):
            logger.info(
                "Ignoring disabled MC ticket command from QQ %s in group %s",
                parsed["sender_id"],
                parsed["group_id"],
            )
            return

        help_match = re.fullmatch(r"/(?:help|帮助)(?:\s+(.+))?", normalized_content, re.IGNORECASE)
        if help_match:
            category = (help_match.group(1) or "").strip()
            if not category:
                lines = ["帮助菜单："]
                lines.extend(f"{name}：{description}" for name, description in self._HELP_CATEGORIES.items())
                lines.append("输入 /help <子菜单名> 查看具体指令。")
                await self._send_group_text(self.config, parsed["group_id"], "\n".join(lines))
            else:
                detail = self._HELP_DETAILS.get(category)
                await self._send_group_text(
                    self.config,
                    parsed["group_id"],
                    detail or "未找到该帮助分类，请输入 /help 查看分类。",
                )
            return

        user_group_match = re.fullmatch(
            r"/用户组(?:\s+(pro|plus)\s+#?(\d+))?",
            normalized_content,
            re.IGNORECASE,
        )
        if user_group_match:
            requested_group = user_group_match.group(1)
            target_id = user_group_match.group(2)
            if requested_group and target_id:
                if not self.db.is_bot_admin(parsed["sender_id"]):
                    await self._send_group_text(self.config, parsed["group_id"], "只有 bot 管理员可以设置用户组。")
                else:
                    self.db.set_user_group(target_id, requested_group.lower())
                    await self._send_group_text(
                        self.config,
                        parsed["group_id"],
                        f"IGNG 用户 {target_id} 已设置为 {requested_group.lower()} 组。",
                    )
            else:
                group_name = self.db.get_user_group(parsed["sender_id"])
                await self._send_group_text(self.config, parsed["group_id"], f"当前用户组：{group_name}")
            return

        is_command = False

        if content and not is_command:
            if re.search(r"/聊天模式", normalized_content):
                role = parsed.get("sender_role", "member")
                if role in ("owner", "admin") or self.db.is_bot_admin(parsed["sender_id"]):
                    new_state = self.db.toggle_chat_mode(parsed["group_id"])
                    await self._send_group_text(
                        self.config,
                        parsed["group_id"],
                        "聊天模式已开启" if new_state else "聊天模式已关闭",
                    )
                is_command = True

        if is_command:
            return
        if parsed.get("is_self", False):
            return

        if await self._handle_auto_plus_one(parsed, content):
            return

        if not content:
            return

        group_config = self.db.get_group_config(parsed["group_id"]) or {}
        chat_mode_enabled = bool(group_config.get("is_chat_mode"))
        textual_direct_alias = self._has_textual_direct_alias(parsed)
        direct_mention = (
            self._is_direct_mention(parsed)
            or textual_direct_alias
        )
        mentioned_user_ids = {
            str(item.get("qq"))
            for item in parsed.get("message_structure", [])
            if item.get("type") == "at" and item.get("qq") is not None
        }
        if textual_direct_alias:
            # Keep the structured field useful to the model even when a client
            # sent a plain-text @云萤/莹宝 instead of a real OneBot at segment.
            mentioned_user_ids.add(str(self.config.BOT_USER_ID))
        # Direct requests must work regardless of the optional proactive chat
        # mode. The mode only controls whether ordinary group messages enter
        # the LLM decision flow.
        if not direct_mention and not chat_mode_enabled:
            return
        if self._should_skip_non_direct_chat_message(
            parsed,
            direct_mention,
            mentioned_user_ids,
            chat_mode_enabled,
        ):
            logger.info(
                "Skipping non-directed chat message in group %s: msg=%s mentions=%s files=%s",
                parsed["group_id"], parsed["msg_id"], sorted(mentioned_user_ids), bool(parsed.get("files")),
            )
            return

        ctx = MessageContext(
            group_id=parsed["group_id"],
            sender_id=parsed["sender_id"],
            sender_name=self._get_sender_name(data),
            msg_id=parsed["msg_id"],
            rich_text=content,
            direct_mention=direct_mention,
            files=parsed.get("files", []),
            mentioned_user_ids=mentioned_user_ids,
        )
        self._enqueue_chat(ctx)

    async def _handle_auto_plus_one(self, parsed: dict, content: str) -> bool:
        """Repeat the first duplicate plain-text message, then silence analysis until text changes."""
        group_id = parsed["group_id"]
        clean_text = self._normalize_plus_one_text(content, parsed.get("files"))
        if not clean_text:
            return False

        previous = self.db.get_previous_non_self_message(group_id, parsed["msg_id"])
        previous_text = ""
        if previous and not previous.get("is_recalled"):
            previous_text = self._normalize_plus_one_text(
                previous.get("message_content") or previous.get("plain_text_content") or "",
                None,
            )
        active_text = self._plus_one_states.get(group_id)

        if active_text == clean_text:
            logger.info("Auto +1 suppressing repeated message in group %s: %s", group_id, clean_text[:50])
            return True

        if active_text and active_text != clean_text:
            self._plus_one_states.pop(group_id, None)
            await self._flush_plus_one_queue(group_id)

        if previous_text == clean_text:
            self._plus_one_states[group_id] = clean_text
            logger.info("Auto +1 triggered in group %s: %s", group_id, clean_text[:50])
            await self._send_group_text(
                self.config,
                group_id,
                clean_text,
                message_source=OUTBOUND_SOURCE_AUTO_PLUS_ONE,
            )
            return True

        return False

    @staticmethod
    def _normalize_plus_one_text(text: str, files: list[dict] | None) -> str:
        if files:
            return ""
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if not normalized or normalized.startswith("/"):
            return ""
        blocked = {
            "[图片]", "[表情]", "[贴纸]", "[视频]", "[文件]", "[语音]",
            "[聊天记录]", "[转发消息]", "图片", "表情", "贴纸", "视频", "文件", "语音",
        }
        if normalized in blocked:
            return ""
        if re.search(r"\[(?:图片|表情|贴纸|视频|文件|语音|聊天记录|转发消息)\]", normalized):
            return ""
        return normalized

    async def _flush_plus_one_queue(self, group_id: int):
        queued = self._plus_one_queued_decisions.pop(group_id, [])
        for ctx, decision in queued:
            if await self.chat_service.apply_decision(ctx, decision):
                self._chat_last_reply_at[group_id] = time.monotonic()

    def _enqueue_chat(self, ctx: MessageContext):
        """Serialize analysis and merge all messages during one run into one next task."""
        group_id = ctx.group_id
        worker = self._chat_workers.get(group_id)
        if worker is not None and not worker.done():
            if group_id not in self._chat_pending:
                self._chat_pending_boundaries[group_id] = ctx.msg_id
                logger.info(
                    "Opening pending chat batch for group %s at msg %s",
                    group_id,
                    ctx.msg_id,
                )
            else:
                logger.info("Merging message %s into pending chat batch for group %s", ctx.msg_id, group_id)
            self._chat_pending[group_id] = ctx
        else:
            self._chat_pending[group_id] = ctx
            self._chat_pending_boundaries.pop(group_id, None)

        if worker is None or worker.done():
            self._chat_workers[group_id] = asyncio.create_task(
                self._run_chat_worker(group_id),
                name=f"chat-worker-{group_id}",
            )

    async def _run_chat_worker(self, group_id: int):
        try:
            while True:
                if group_id not in self._chat_pending:
                    return
                debounce_seconds = max(0.0, self.config.CHAT_DEBOUNCE_SECONDS)
                if debounce_seconds:
                    await asyncio.sleep(debounce_seconds)
                ctx = self._chat_pending.pop(group_id, None)
                if ctx is None:
                    return
                history_start_msg_id = self._chat_pending_boundaries.pop(group_id, None)

                group_config = self.db.get_group_config(group_id) or {}
                if not group_config.get("is_chat_mode") and not ctx.direct_mention:
                    continue

                if not ctx.direct_mention:
                    last_reply_at = self._chat_last_reply_at.get(group_id)
                    cooldown = max(0.0, self.config.CHAT_NON_DIRECT_COOLDOWN_SECONDS)
                    if last_reply_at is not None and time.monotonic() - last_reply_at < cooldown:
                        logger.info(
                            "Skipping non-directed chat message during cooldown: group=%s msg=%s",
                            group_id,
                            ctx.msg_id,
                        )
                        continue

                logger.info("Starting chat analysis for group %s msg %s", group_id, ctx.msg_id)
                decision = await self.chat_service.maybe_reply(ctx, history_start_msg_id)
                if decision.get("should_reply"):
                    if group_id in self._plus_one_states:
                        self._plus_one_queued_decisions.setdefault(group_id, []).append((ctx, decision))
                        logger.info("Queued bot reply during auto +1 in group %s", group_id)
                    else:
                        if await self.chat_service.apply_decision(ctx, decision):
                            self._chat_last_reply_at[group_id] = time.monotonic()
                logger.info("Finished chat analysis for group %s msg %s", group_id, ctx.msg_id)
        except Exception:
            logger.exception("Chat worker failed for group %s", group_id)
        finally:
            self._chat_workers.pop(group_id, None)
            if group_id in self._chat_pending:
                self._chat_workers[group_id] = asyncio.create_task(
                    self._run_chat_worker(group_id),
                    name=f"chat-worker-{group_id}",
                )

    def _is_direct_mention(self, parsed: dict) -> bool:
        for item in parsed.get("message_structure", []):
            if item.get("type") == "at" and str(item.get("qq")) == str(self.config.BOT_USER_ID):
                return True
        reply_to = parsed.get("reply_to_msg_id")
        if not reply_to:
            return False
        quoted = self.db.get_message_by_msg_id(parsed.get("group_id"), reply_to)
        if quoted and (quoted.get("is_self") or str(quoted.get("sender_id")) == str(self.config.BOT_USER_ID)):
            return True
        return False

    def _has_textual_direct_alias(self, parsed: dict) -> bool:
        """Recognize high-confidence plain-text calls to Yunying.

        Some clients render a nickname-like @ as ordinary text, so OneBot does
        not provide an ``at`` segment.  Do not treat every occurrence of the
        bot's name as a call: ``你看莹宝也说抽哎`` is still a message to another
        group member.  Accept explicit ``@alias`` anywhere and a bare alias only
        at the start when the remainder looks like a direct request.
        """
        text = str(parsed.get("message_content") or parsed.get("plain_text_content") or "").strip()
        if not text:
            return False
        aliases = tuple(
            alias for alias in getattr(self.config, "CHAT_DIRECT_ALIASES", ()) if str(alias).strip()
        )
        if not aliases:
            return False
        alias_group = "|".join(re.escape(str(alias).strip()) for alias in aliases)
        separator = r"(?=$|[\s,，。！？!?：:、])"
        if re.search(rf"(?<!\S)@(?:{alias_group}){separator}", text, flags=re.IGNORECASE):
            return True

        direct_cues = (
            "你", "能", "可以", "帮", "请", "问", "在吗", "在不", "告诉", "回答",
            "为什么", "怎么", "什么", "是否", "有没有", "要不要", "会不会", "能否",
            "抽", "看", "听", "来", "给我", "觉得", "认为",
        )
        cue_group = "|".join(re.escape(cue) for cue in direct_cues)
        return bool(
            re.match(
                rf"^\s*(?:{alias_group})(?:\s*[,，:：]\s*|\s+|(?=(?:{cue_group})))",
                text,
                flags=re.IGNORECASE,
            )
        )

    def _should_skip_non_direct_chat_message(
        self,
        parsed: dict,
        direct_mention: bool,
        mentioned_user_ids: set[str],
        chat_mode_enabled: bool = False,
    ) -> bool:
        if direct_mention:
            return False
        # Do not intercept a message explicitly addressed to another QQ user.
        # Plain topic messages may enter the LLM only when the admin enabled
        # proactive chat mode; the model then decides whether a reply adds
        # value, with the worker cooldown preventing reply storms.
        if mentioned_user_ids:
            return True
        return not chat_mode_enabled

    def _normalize_command_text(self, text: str) -> str:
        clean_text = (text or "").strip()
        clean_text = re.sub(r"^@[^\s/]+\s*", "", clean_text).strip()
        return clean_text

    @staticmethod
    def _is_disabled_mc_ticket_command(text: str) -> bool:
        return bool(re.match(r"^/服务器反馈(?:\s|$)", text or "", re.IGNORECASE))

    def _get_sender_name(self, data: dict) -> str:
        sender = data.get("sender") or {}
        if isinstance(sender, dict):
            return sender.get("card") or sender.get("nickname") or str(data.get("user_id"))
        return str(data.get("user_id"))

    def _json_dump(self, data) -> str | None:
        return None if not data else json.dumps(data, ensure_ascii=False)


def main():
    app = App()
    app.startup()
    client = OneBotClient(
        app.config,
        app.handle_raw_message,
        app.handle_recall_event,
    )
    logger.info("Starting IGNGbot v3 listener...")
    client.start()


if __name__ == "__main__":
    main()
