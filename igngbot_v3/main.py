import asyncio
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from concurrent.futures import Future
from logging.handlers import RotatingFileHandler

from .call_log_db import close_call_log_pool, ensure_call_logs_table
from .content_review import ContentReviewer
from .chat_service import ChatService, MessageContext
from .config import Config
from .db import DBHandler
from .avatar_generator import AvatarGenerator
from .igng_query import IGNGQueryHandler
from .message_parser import parse_message
from .mc_report_notifications import McReportNotifier
from .onebot_client import OneBotClient
from .onebot_api import send_group_text
from .sticker_generator import StickerGenerator
from .storage import StorageHandler
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
    def __init__(self):
        self.config = Config()
        self.db = DBHandler(self.config)
        self.chat_service = ChatService(self.config, self.db)
        self.storage = StorageHandler(self.config)
        self.reviewer = ContentReviewer(self.config)
        self.avatar_gen = AvatarGenerator(self.config)
        self.sticker_gen = StickerGenerator(self.config)
        self.igng_query = IGNGQueryHandler(self.config)
        self.mc_report_notifier = McReportNotifier(self.config)
        self.content_review_groups = []
        self._chat_workers = {}
        self._chat_pending = {}
        self._chat_pending_boundaries = {}
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
        self.avatar_gen.start()
        self.sticker_gen.start()
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
        await self.mc_report_notifier.start()

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
        content = parsed.get("message_content", "").strip()
        normalized_content = self._normalize_command_text(content)
        is_command = False

        if content:
            if re.search(r"/聊天模式", normalized_content):
                role = parsed.get("sender_role", "member")
                if role in ("owner", "admin") or self.config.is_admin_user(parsed["sender_id"]):
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
                    self.config.is_admin_user(parsed["sender_id"])
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

        if not is_command:
            is_command = bool(self.sticker_gen.handle_command(parsed))
        if not is_command:
            is_command = bool(self.avatar_gen.handle_command(parsed))
        if not is_command:
            is_command = bool(self.igng_query.handle_command(parsed))

        file_url = None
        file_type = None
        audio_file_path = None
        stored_attachments = []
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
                    attachment = {
                        "type": file_info["type"],
                        "original_file": file_info.get("file", ""),
                        "original_url": file_info.get("url", ""),
                        "stored_path": stored_path,
                        "name": file_info.get("name", ""),
                        "transcript": file_info.get("transcript", ""),
                    }
                    stored_attachments.append(attachment)
                    if file_url is None:
                        file_url = stored_path
                        file_type = file_info["type"]
                    if file_info["type"] in ("audio", "record") and audio_file_path is None:
                        audio_file_path = stored_path

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
                datetime.fromtimestamp(parsed["created_at"])
                if parsed.get("created_at")
                else None
            ),
            is_self=parsed.get("is_self", False),
            audio_file_path=audio_file_path,
            audio_transcript=parsed.get("audio_transcript", ""),
        )

        self.sticker_gen.trigger(
            parsed["group_id"],
            parsed.get("message_content", ""),
            parsed["sender_id"],
        )
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
        if not group_config.get("is_chat_mode"):
            return

        ctx = MessageContext(
            group_id=parsed["group_id"],
            sender_id=parsed["sender_id"],
            sender_name=self._get_sender_name(data),
            msg_id=parsed["msg_id"],
            rich_text=content,
            direct_mention=self._is_direct_mention(parsed),
            files=parsed.get("files", []),
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
            await self.chat_service.apply_decision(ctx, decision)

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
                ctx = self._chat_pending.pop(group_id, None)
                if ctx is None:
                    return
                history_start_msg_id = self._chat_pending_boundaries.pop(group_id, None)

                group_config = self.db.get_group_config(group_id) or {}
                if not group_config.get("is_chat_mode"):
                    continue

                logger.info("Starting chat analysis for group %s msg %s", group_id, ctx.msg_id)
                decision = await self.chat_service.maybe_reply(ctx, history_start_msg_id)
                if decision.get("should_reply") or decision.get("affinity_updates"):
                    if group_id in self._plus_one_states:
                        self._plus_one_queued_decisions.setdefault(group_id, []).append((ctx, decision))
                        logger.info("Queued bot reply during auto +1 in group %s", group_id)
                    else:
                        await self.chat_service.apply_decision(ctx, decision)
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
        return False

    def _normalize_command_text(self, text: str) -> str:
        clean_text = (text or "").strip()
        clean_text = re.sub(r"^@[^\s/]+\s*", "", clean_text).strip()
        return clean_text

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
