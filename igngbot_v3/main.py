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
from .content_review import ContentReviewer
from .chat_service import ChatService, MessageContext
from .config import Config
from .db import DBHandler
from .media_repository_worker import MediaRepositoryWorker
from .message_parser import parse_message
from .mc_ticket_notifications import McTicketNotifier
from .system_prompt_commands import SystemPromptCommandHandler
from .onebot_client import OneBotClient
from .onebot_api import send_group_text
from .storage import StorageHandler
from .media_text import MediaTextExtractor, MediaTextResult, append_media_text
from .timeutil import unix_to_utc_naive
from .user_config_db import close_pool as close_user_config_pool
from .user_config_db import ensure_group_configs_table, ensure_user_config_table


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
        "聊天": "群聊模式和性格设置",
        "系统提示词": "聊天模式附加系统提示词",
        "管理": "通知和群聊总结等管理员功能",
    }

    _HELP_DETAILS = {
        "聊天": (
            "聊天帮助\n"
            "聊天模式使用本地模型；在群聊中负责自然对话。\n"
            "/聊天模式\n切换当前群聊天模式，仅群主、群管理员或 bot 管理员可用。\n"
            "/性格\n查看当前性格和可用性格。\n"
            "/性格 <名称>\n切换当前群性格，仅管理员可用。"
        ),
        "系统提示词": (
            "系统提示词帮助\n"
            "本功能仅影响聊天模式。用户附加的提示词会拼接到 bot 系统提示词中段，"
            "对所有群、所有聊天模式回复全局生效。\n"
            "/系统提示词 附加 <内容>\n"
            "提交一条附加系统提示词。不会直接入库，先调用云端 LLM 审核："
            "检查是否合法（不得诱导违规行为），以及是否有意义（整活可以，乱填不行）。"
            "审核通过后才会启用。\n"
            "/系统提示词 列表\n"
            "查看当前已拼接、处于启用状态的全部附加系统提示词（含 ID 与提交者）。\n"
            "/系统提示词 修改 #id <内容>\n"
            "修改自己提交的附加提示词；bot 管理员可修改任何人的。"
            "修改同样需要审核，不通过则继续使用原版本。\n"
            "/系统提示词 关闭 #id\n"
            "关闭自己提交的附加提示词；bot 管理员可关闭任何人的。关闭后不再参与拼接。\n"
            "示例：\n"
            "/系统提示词 附加 偶尔说一句神了\n"
            "/系统提示词 列表\n"
            "/系统提示词 修改 #3 更毒舌一点\n"
            "/系统提示词 关闭 #3"
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
        self.chat_service = ChatService(self.config, self.db)
        self.storage = StorageHandler(self.config)
        self.media_text = MediaTextExtractor(self.config)
        self.reviewer = ContentReviewer(self.config)
        self.media_repository = MediaRepositoryWorker(self.config, self.db)
        self.mc_ticket_notifier = McTicketNotifier(self.config)
        self.system_prompt_commands = SystemPromptCommandHandler(self.config, self.db)
        self.content_review_groups = []
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

    def startup(self):
        self.db.connect()
        self.db.init_table()
        self.db.init_group_configs_table()
        if self.config.CONTENT_REVIEW_ENABLED:
            self.content_review_groups = self.db.get_content_review_groups()
            for legacy_gid in (1000000004, 1000000005):
                if legacy_gid not in self.content_review_groups:
                    self.db.set_content_review(legacy_gid, True)
                    self.content_review_groups.append(legacy_gid)
        else:
            self.content_review_groups = []
            logger.info("Content review disabled by configuration")
        self.storage.check_available()
        if self.config.CONTENT_REVIEW_ENABLED:
            self.reviewer.mark_all_existing_approved()
            self.reviewer.start()
        self.media_repository.start()
        self.loop_thread.start()
        self._run_coro_sync(self._startup_async()).result()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run_coro_sync(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _startup_async(self):
        await ensure_user_config_table()
        await ensure_group_configs_table()
        await ensure_call_logs_table()
        await close_user_config_pool()
        await close_call_log_pool()
        await self.mc_ticket_notifier.start()

    def handle_raw_message(self, data: dict):
        future = self._run_coro_sync(self._handle_raw_message_async(data))
        future.add_done_callback(self._log_future_error)

    def _log_future_error(self, future: Future):
        try:
            future.result()
        except Exception:
            logger.exception("Message processing failed")

    async def _handle_raw_message_async(self, data: dict):
        parsed = parse_message(data)
        if parsed is None:
            return

        self.db.ensure_group_exists(parsed["group_id"])
        if parsed.get("is_self") and self.db.get_message_by_msg_id(
            parsed["group_id"], parsed["msg_id"]
        ):
            return
        # Persist incoming messages and download attachments before command or
        # task handling so continuous task mode can use stable NAS paths and
        # media-derived text can be included in the same LLM turn.
        file_url = None
        file_type = None
        audio_file_path = None
        stored_attachments = []
        extracted_media = []
        if parsed.get("files"):
            for file_info in parsed["files"]:
                if not file_info.get("url"):
                    continue
                name = file_info.get("file") or file_info.get("name", "unknown")
                file_name = f"{parsed['msg_id']}_{name}"
                stored_path = self.storage.download_and_store(
                    file_info["url"],
                    parsed["group_id"],
                    file_name,
                    file_info["type"],
                )
                if stored_path:
                    file_info["stored_path"] = stored_path
                    media_result = None
                    try:
                        if file_info["type"] == "image":
                            media_result = await asyncio.to_thread(
                                self.media_text.extract,
                                file_info["type"],
                                stored_path,
                            )
                        elif file_info["type"] in ("audio", "record"):
                            existing_transcript = str(file_info.get("transcript") or "").strip()
                            if existing_transcript:
                                media_result = MediaTextResult(
                                    text=existing_transcript,
                                    status="provided",
                                    backend="onebot",
                                )
                            else:
                                media_result = await asyncio.to_thread(
                                    self.media_text.extract,
                                    file_info["type"],
                                    stored_path,
                                )
                    except Exception as exc:
                        logger.exception("Media text extraction failed for %s", stored_path)
                        media_result = MediaTextResult(
                            status="unavailable",
                            backend="error",
                            error=str(exc),
                        )
                    if media_result:
                        file_info["text_extraction_status"] = media_result.status
                        file_info["text_extraction_backend"] = media_result.backend
                        if media_result.text and media_result.backend != "onebot":
                            if file_info["type"] == "image":
                                file_info["ocr_text"] = media_result.text
                            else:
                                file_info["transcript"] = media_result.text
                            extracted_media.append((file_info, media_result))
                    attachment = {
                        "type": file_info["type"],
                        "original_file": file_info.get("file", ""),
                        "original_url": file_info.get("url", ""),
                        "stored_path": stored_path,
                        "name": file_info.get("name", ""),
                        "transcript": file_info.get("transcript", ""),
                        "ocr_text": file_info.get("ocr_text", ""),
                    }
                    if media_result:
                        attachment["text_extraction_status"] = media_result.status
                        attachment["text_extraction_backend"] = media_result.backend
                    stored_attachments.append(attachment)
                    if file_url is None:
                        file_url = stored_path
                        file_type = file_info["type"]
                    if file_info["type"] in ("audio", "record") and audio_file_path is None:
                        audio_file_path = stored_path

        append_media_text(parsed, extracted_media)
        content = parsed.get("message_content", "").strip()
        normalized_content = self._normalize_command_text(content)

        if not self.db.get_message_by_msg_id(parsed["group_id"], parsed["msg_id"]):
            self.db.insert_message(
                group_id=parsed["group_id"],
                sender_id=parsed["sender_id"],
                message_content=parsed["message_content"],
                plain_text_content=parsed.get("plain_text_content", ""),
                message_structure=self._json_dump(parsed.get("message_structure", [])),
                attachments_json=self._json_dump(stored_attachments) if stored_attachments else None,
                reply_to_msg_id=parsed["reply_to_msg_id"],
                msg_id=parsed["msg_id"],
                file_url=file_url,
                file_type=file_type,
                created_at=(
                    unix_to_utc_naive(parsed["created_at"])
                    if parsed.get("created_at")
                    else None
                ),
                is_self=parsed.get("is_self", False),
                audio_file_path=audio_file_path,
                audio_transcript=parsed.get("audio_transcript", ""),
            )

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
                await send_group_text(self.config, parsed["group_id"], "\n".join(lines))
            else:
                detail = self._HELP_DETAILS.get(category)
                await send_group_text(
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
                    await send_group_text(self.config, parsed["group_id"], "只有 bot 管理员可以设置用户组。")
                else:
                    self.db.set_user_group(target_id, requested_group.lower())
                    await send_group_text(
                        self.config,
                        parsed["group_id"],
                        f"IGNG 用户 {target_id} 已设置为 {requested_group.lower()} 组。",
                    )
            else:
                group_name = self.db.get_user_group(parsed["sender_id"])
                await send_group_text(self.config, parsed["group_id"], f"当前用户组：{group_name}")
            return

        is_command = False


        if content and not is_command:
            if await self.system_prompt_commands.handle(parsed, normalized_content):
                is_command = True

        if content and not is_command:
            if re.search(r"/聊天模式", normalized_content):
                role = parsed.get("sender_role", "member")
                if role in ("owner", "admin") or self.db.is_bot_admin(parsed["sender_id"]):
                    new_state = self.db.toggle_chat_mode(parsed["group_id"])
                    await send_group_text(
                        self.config,
                        parsed["group_id"],
                        "聊天模式已开启" if new_state else "聊天模式已关闭",
                    )
                is_command = True

            personality_match = re.fullmatch(r"/性格(?:\s+(.+))?", normalized_content)
            if personality_match:
                requested_name = (personality_match.group(1) or "").strip()
                can_manage_personality = (
                    self.db.is_bot_admin(parsed["sender_id"])
                    or
                    parsed.get("sender_role") in ("owner", "admin")
                )
                if requested_name and can_manage_personality:
                    if self.db.activate_personality(parsed["group_id"], requested_name):
                        await send_group_text(
                            self.config,
                            parsed["group_id"],
                            f"当前性格已切换为：{requested_name}",
                        )
                    else:
                        names = [row["name"] for row in self.db.list_personalities()]
                        await send_group_text(
                            self.config,
                            parsed["group_id"],
                            f"没有找到性格「{requested_name}」。可选：{'、'.join(names)}",
                        )
                elif requested_name and not can_manage_personality:
                    await send_group_text(
                        self.config,
                        parsed["group_id"],
                        "只有管理员可以切换性格。",
                    )
                else:
                    current = self.db.get_active_personality(parsed["group_id"])
                    names = [row["name"] for row in self.db.list_personalities()]
                    await send_group_text(
                        self.config,
                        parsed["group_id"],
                        f"当前性格：{current['name'] if current else '未设置'}；可选：{'、'.join(names)}",
                    )
                is_command = True



        if self.config.CONTENT_REVIEW_ENABLED and parsed["group_id"] in self.content_review_groups:
            self.reviewer.trigger()

        if is_command:
            return
        if parsed.get("is_self", False):
            return

        if await self._handle_auto_plus_one(parsed, content):
            return

        if not content:
            return

        group_config = self.db.get_group_config(parsed["group_id"]) or {}
        if parsed.get("conversation_type") != "private" and not group_config.get("is_chat_mode"):
            return

        textual_direct_alias = self._has_textual_direct_alias(parsed)
        direct_mention = (
            parsed.get("conversation_type") == "private"
            or self._is_direct_mention(parsed)
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
        if self._should_skip_non_direct_chat_message(parsed, direct_mention, mentioned_user_ids):
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
        previous_text = self._normalize_plus_one_text(
            (previous or {}).get("message_content") or (previous or {}).get("plain_text_content") or "",
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
            await send_group_text(self.config, group_id, clean_text)
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
                if not group_config.get("is_chat_mode"):
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
                if decision.get("should_reply") or decision.get("affinity_updates"):
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
    ) -> bool:
        if parsed.get("conversation_type") == "private" or direct_mention:
            return False
        # In group chat, a question or a topic continuation is still a message
        # between group members unless the sender explicitly addressed Yunying.
        # Do this deterministically; the small chat model is not a reliable
        # addressee classifier.
        return True

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
    client = OneBotClient(app.config, app.handle_raw_message)
    logger.info("Starting IGNGbot v3 listener...")
    client.start()


if __name__ == "__main__":
    main()
