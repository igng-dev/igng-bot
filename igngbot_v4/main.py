"""OneBot/media sidecar for a native DSH Profile. No conversation LLM call is made here."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import signal
import threading
import uuid
from aiohttp import ClientSession, ClientTimeout, web
from igngbot_shared.config import Config
from igngbot_shared.db import DBHandler
from igngbot_shared.storage import StorageHandler
from igngbot_shared.message_ingest import persist_message
from igngbot_shared.message_parser import parse_message
from igngbot_shared.onebot_client import OneBotClient
from igngbot_shared.mc_ticket_notifications import McTicketNotifier
from igngbot_shared.timeutil import unix_to_utc_naive
from igngbot_shared.call_log_db import mirror_call_to_site, close_call_log_pool
from .ai_records import mirror_native_record, historical_record
from .broker_client import BrokerClient
from .journal import Journal, decode, encode
from .migrate import connect, migrate
from .settings import Settings, signed_conversation
from .terminal import TerminalGateway
from .views import message_view, row_images, safe_structure

logger = logging.getLogger("yunying.infrastructure")


_ANNOUNCEMENT_NOTICE_TYPES = {
    "group_announcement",
    "group_announcement_publish",
    "group_notice",
    "group_notice_publish",
}
_ANNOUNCEMENT_SUBTYPES = {"announcement", "group_announcement", "publish", "published"}
_BOT_NOTICE_TYPES = {"group_admin", "group_ban", "group_decrease", "group_increase"}


def _qq_id(value):
    text = str(value or "").strip()
    return text if re.fullmatch(r"[1-9][0-9]{0,19}", text) else ""


def _notice_text(value, limit=4000):
    if isinstance(value, dict):
        for key in ("content", "text", "body", "message", "title"):
            result = _notice_text(value.get(key), limit)
            if result:
                return result
        return ""
    if isinstance(value, list):
        return " ".join(filter(None, (_notice_text(item, limit) for item in value)))[:limit]
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _notice_value(raw, nested, *keys):
    for source in (raw, nested):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _notice_operator(raw, nested, include_user=True):
    sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    nested_sender = nested.get("sender") if isinstance(nested.get("sender"), dict) else {}
    operator_keys = ["operator_id", "author_id", "sender_id"]
    if include_user:
        operator_keys.append("user_id")
    operator_id = _qq_id(_notice_value(raw, nested, *operator_keys))
    operator_name = _notice_text(
        sender.get("card") or sender.get("nickname")
        or nested_sender.get("card") or nested_sender.get("nickname")
        or _notice_value(raw, nested, "operator_name", "author_name", "user_name"),
        120,
    )
    return operator_id, operator_name or "QQ群系统"


def _notice_time(raw):
    try:
        return int(float(raw.get("time") or 0) * 1000)
    except (TypeError, ValueError):
        return 0


def normalize_notice(raw, event_id, bot_id=0):
    """Convert selected OneBot notices into bounded, untrusted model data.

    The model never receives the raw notice.  Unknown notices intentionally
    remain invisible to the Social Agent, while the raw ingress row remains
    available to operators for troubleshooting.
    """
    if not isinstance(raw, dict) or raw.get("post_type") != "notice":
        return None
    group_id = _qq_id(raw.get("group_id"))
    if not group_id:
        return None
    notice_type = str(raw.get("notice_type") or "").strip().lower()
    subtype = str(raw.get("sub_type") or "").strip().lower()
    nested = next(
        (
            value
            for key in ("announcement", "group_announcement", "data")
            if isinstance((value := raw.get(key)), dict)
        ),
        {},
    )
    operator_id, operator_name = _notice_operator(raw, nested)
    notice_id = str(
        _notice_value(raw, nested, "notice_id", "announcement_id", "id", "message_id")
        or event_id
        or ""
    )[:96]
    base = {
        "eventId": str(event_id),
        "kind": "system_notice",
        "isSystemEvent": True,
        "isSelf": False,
        "isRecalled": False,
        "messageId": notice_id,
        "userId": operator_id or "0",
        "sender": operator_name,
        "time": _notice_time(raw),
        "media": [],
        "structure": [],
    }

    announcement_value = raw.get("announcement") or raw.get("group_announcement")
    is_announcement = (
        notice_type in _ANNOUNCEMENT_NOTICE_TYPES
        or (notice_type == "notify" and subtype in _ANNOUNCEMENT_SUBTYPES)
        or isinstance(announcement_value, (dict, str))
    )
    if is_announcement:
        announcement = announcement_value if isinstance(announcement_value, dict) else nested
        title = _notice_text(
            _notice_value(raw, announcement, "title", "subject", "name"),
            240,
        )
        content = _notice_text(
            _notice_value(raw, announcement, "content", "text", "body", "message", "description"),
            4000,
        )
        if not title and not content:
            return None
        body = "\n".join(part for part in (title, content) if part)
        label = "[群公告]" + (f" {title}" if title else "")
        return {
            **base,
            "noticeType": "group_announcement",
            "announcementId": notice_id,
            "operatorId": operator_id or None,
            "operatorName": operator_name,
            "title": title,
            "content": content,
            "text": f"{label}\n{content}" if content else label,
            "plain": body,
        }

    target_id = _qq_id(_notice_value(raw, nested, "user_id", "target_id"))
    current_bot_id = _qq_id(bot_id) or _qq_id(raw.get("self_id"))
    if notice_type in _BOT_NOTICE_TYPES and current_bot_id and target_id == current_bot_id:
        action_operator_id, action_operator_name = _notice_operator(raw, nested, include_user=False)
        if notice_type == "group_admin":
            state = "授予管理员" if subtype in {"set", "grant", "enable"} else "取消管理员"
            bot_notice = "bot_admin_changed"
        elif notice_type == "group_ban":
            state = "被禁言" if subtype in {"ban", "mute", "set"} else "解除禁言"
            bot_notice = "bot_mute_changed"
        elif notice_type == "group_decrease":
            state = "被移出群" if subtype in {"kick", "kicked", "leave", "left"} else "离开群聊"
            bot_notice = "bot_removed"
        else:
            state = "加入群聊"
            bot_notice = "bot_joined"
        return {
            **base,
            "noticeType": bot_notice,
            "targetId": target_id,
            "operatorId": action_operator_id or None,
            "operatorName": action_operator_name,
            "text": f"[QQ状态] 机器人{state}",
            "plain": f"机器人{state}",
        }
    return None


class Infrastructure:
    def __init__(self, config=None, settings=None):
        self.config = config or Config()
        self.settings = settings or Settings.from_env()
        self.db = DBHandler(self.config, legacy_compat=False)
        self.storage = StorageHandler(self.config)
        self.conn = None
        self.journal = None
        self.http = None
        self.loop = None
        self.client = None
        self.broker = None
        self.terminal = None
        self._send_lock = asyncio.Lock()
        self._stopping = False
        self._journal_signal = asyncio.Event()
        self._group_modes = {}
        self._delivery_signal = asyncio.Event()

    async def enqueue(self, raw):
        if raw.get("group_id"):
            raw = {**raw, "_yunying_chat_mode": self.group_policy(int(raw["group_id"]))["chatMode"]}
        key, _ = self.journal.enqueue(raw)
        # Durability precedes any slow media download or DSH contact.
        self._journal_signal.set()
        return key

    def callback(self, raw):
        if self._stopping:
            return
        # Dispatched to the async loop without blocking the WebSocket reception thread.
        future = asyncio.run_coroutine_threadsafe(self.enqueue(raw), self.loop)
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() and f.exception() else None)
        if raw.get("self_id") and not self.config.BOT_USER_ID:
            self.config.BOT_USER_ID = int(raw["self_id"])

    async def command(self, raw, parsed, key, event_id):
        text = (parsed.get("message_content") or "").strip()
        sender = parsed["sender_id"]
        central_admin = lambda: self.db.is_bot_admin(sender)
        message = None
        handled = False
        if text in {"/help", "/帮助"}:
            message = "/聊天模式 [开启|关闭]：群管理员或 bot 管理员控制自主参与。\n直接 @ 或回复云萤即可聊天。"
            handled = True
        elif re.fullmatch(r"/聊天模式(?:\s+(开启|关闭|on|off))?", text, re.I) and key.startswith("group:"):
            handled = True
            if parsed.get("sender_role") in {"owner", "admin"} or central_admin():
                option = text.split()[1].lower() if len(text.split()) > 1 else None
                enabled = self.journal.group_control(event_id, signed_conversation(key), "is_chat_mode",
                    None if option is None else option in {"开启", "on"})
                message = "聊天模式已开启，云萤可自主参与。" if enabled else "聊天模式已关闭；消息照常记录，@ 或回复云萤仍可呼叫。"
            else:
                message = "需要群管理员或 bot 管理员权限。"
        if message:
            await self.send({"key": key, "requestId": f"command:{event_id}", "message": message}, owner_command=True)
        return {"commandHandled": handled}

    def group_policy(self, gid):
        # Read with the autocommit lease connection so website updates cannot be cached.
        with self.conn.cursor() as cur:
            cur.execute("SELECT is_chat_mode FROM group_configs WHERE group_id=%s", (gid,))
            row = cur.fetchone()
            return {"chatMode": bool(row and row.get("is_chat_mode"))}

    async def _onebot_action(self, action, payload):
        """Call one fixed read-only OneBot action and discard provider details."""
        if self.http is None:
            return None
        try:
            async with self.http.post(
                self.config.ONEBOT_HTTP_URL.rstrip("/") + "/" + action,
                json=payload,
                headers={"Authorization": "Bearer " + self.config.ONEBOT_HTTP_TOKEN},
                timeout=ClientTimeout(total=10),
            ) as response:
                body = await response.json()
        except Exception:
            logger.warning("OneBot read capability failed: %s", action)
            return None
        if (
            response.status != 200
            or not isinstance(body, dict)
            or body.get("status") != "ok"
            or body.get("retcode") != 0
        ):
            logger.warning("OneBot read capability rejected: %s", action)
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _member_view(data):
        if not isinstance(data, dict):
            return None
        user_id = _qq_id(data.get("user_id"))
        if not user_id:
            return None
        role = str(data.get("role") or "member")
        if role not in {"owner", "admin", "member"}:
            role = "member"
        try:
            muted_until = int(data.get("shut_up_timestamp") or 0)
        except (TypeError, ValueError):
            muted_until = 0
        return {
            "userId": user_id,
            "nickname": str(data.get("nickname") or "")[:120],
            "card": str(data.get("card") or "")[:120],
            "role": role,
            "title": str(data.get("title") or "")[:120],
            "mutedUntil": muted_until,
            "isMuted": muted_until > 0,
        }

    async def account_status(self):
        login = await self._onebot_action("get_login_info", {})
        status = await self._onebot_action("get_status", {})
        if login is None and status is None:
            return {"ok": False, "error": "QQ 状态暂不可用"}
        user_id = _qq_id(login.get("user_id")) if login else ""
        if user_id and not self.config.BOT_USER_ID:
            self.config.BOT_USER_ID = int(user_id)
        return {
            "ok": True,
            "userId": user_id or _qq_id(self.config.BOT_USER_ID) or None,
            "nickname": str((login or {}).get("nickname") or "")[:120],
            "online": None if status is None or "online" not in status else bool(status.get("online")),
            "good": None if status is None or "good" not in status else bool(status.get("good")),
        }

    async def current_group_info(self, key):
        gid = signed_conversation(key)
        if gid < 0:
            raise web.HTTPBadRequest(text="群状态只适用于群会话")
        data = await self._onebot_action("get_group_info", {"group_id": gid})
        if data is None:
            return {"ok": False, "error": "QQ群状态暂不可用"}
        try:
            member_count = int(data.get("member_count") or 0)
        except (TypeError, ValueError):
            member_count = 0
        try:
            max_member_count = int(data.get("max_member_count") or 0)
        except (TypeError, ValueError):
            max_member_count = 0
        return {
            "ok": True,
            "groupId": str(gid),
            "groupName": str(data.get("group_name") or "")[:160],
            "memberCount": member_count,
            "maxMemberCount": max_member_count,
            "chatMode": self.group_policy(gid)["chatMode"],
        }

    async def current_member_info(self, key, user_id):
        gid = signed_conversation(key)
        if gid < 0:
            raise web.HTTPBadRequest(text="成员状态只适用于群会话")
        user_id = _qq_id(user_id)
        if not user_id:
            raise web.HTTPBadRequest(text="invalid user id")
        bot_id = _qq_id(self.config.BOT_USER_ID)
        if user_id != bot_id:
            with self.db.conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM message_logs WHERE group_id=%s AND sender_id=%s LIMIT 1",
                    (gid, user_id),
                )
                if not cur.fetchone():
                    raise web.HTTPForbidden(text="成员不在当前会话范围")
        data = await self._onebot_action("get_group_member_info", {"group_id": gid, "user_id": int(user_id)})
        member = self._member_view(data)
        if member is None:
            return {"ok": False, "error": "QQ群成员状态暂不可用"}
        return {"ok": True, "groupId": str(gid), "member": member}

    async def bot_permissions(self, key):
        gid = signed_conversation(key)
        if gid < 0:
            raise web.HTTPBadRequest(text="机器人群权限只适用于群会话")
        bot_id = _qq_id(self.config.BOT_USER_ID)
        if not bot_id:
            account = await self.account_status()
            bot_id = _qq_id(account.get("userId"))
        if not bot_id:
            return {"ok": False, "error": "机器人 QQ 状态暂不可用"}
        data = await self._onebot_action(
            "get_group_member_info", {"group_id": gid, "user_id": int(bot_id)}
        )
        member = self._member_view(data)
        if member is None:
            return {"ok": False, "error": "机器人群权限暂不可用"}
        return {
            "ok": True,
            "groupId": str(gid),
            "botUserId": bot_id,
            "role": member["role"],
            "mutedUntil": member["mutedUntil"],
            "isMuted": member["isMuted"],
            "chatMode": self.group_policy(gid)["chatMode"],
        }

    async def notifications(self, key, limit=10):
        limit = min(20, max(1, int(limit)))
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT event_id, raw_event FROM yunying_ingress "
                "WHERE conversation_key=%s ORDER BY id DESC LIMIT %s",
                (key, min(100, limit * 8)),
            )
            rows = cur.fetchall()
        result = []
        for row in rows:
            try:
                raw = decode(row["raw_event"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            notice = normalize_notice(raw, row["event_id"], self.config.BOT_USER_ID)
            if notice:
                result.append(notice)
            if len(result) >= limit:
                break
        result.reverse()
        return {"ok": True, "key": key, "notifications": result}

    def authorized(self, key):
        """Groups use the operator allowlist; private chat needs an IGNG plus+ account."""
        kind, value = key.split(":", 1)
        if kind == "group":
            return value in self.settings.groups
        try:
            return self.db.private_allowed(value)
        except Exception as error:
            logger.warning("Private authorization denied after error: %s", type(error).__name__)
            return False

    def speaking_allowed(self, key, trigger_event_id=None):
        gid = signed_conversation(key)
        if gid < 0 or self.group_policy(gid)["chatMode"]:
            return True
        if not isinstance(trigger_event_id, str):
            return False
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1 FROM yunying_sessions s JOIN yunying_events e ON e.event_id=s.direct_event_id "
                        "AND e.conversation_key=s.conversation_key WHERE s.conversation_key=%s AND s.direct_event_id=%s "
                        "AND s.direct_expires_at>UTC_TIMESTAMP(6)", (key, trigger_event_id))
            return bool(cur.fetchone())

    async def sync_group_modes(self):
        for group in sorted(self.settings.groups):
            enabled = self.group_policy(int(group))
            if self._group_modes.get(group) == enabled:
                continue
            # Trusted chat-mode configuration shares the durable per-conversation FIFO.
            # No fake QQ message is inserted into message_logs, and this never wakes on its own.
            await self.enqueue({"post_type": "yunying_configuration", "notice_type": "yunying_configuration",
                                "group_id": int(group), "nonce": str(uuid.uuid4())})
            self._group_modes[group] = enabled

    async def monitor_group_modes(self):
        while not self._stopping:
            await self.sync_group_modes()
            await asyncio.sleep(5)

    async def prepare(self, row):
        raw = decode(row["raw_event"])
        key = row["conversation_key"]
        event_id = row["event_id"]
        if raw.get("post_type") == "yunying_configuration":
            return {"eventId": event_id, "key": key, "kind": "message", "isConfiguration": True,
                    "commandHandled": True, "isSelf": True, "userId": "0", "sender": "群配置",
                    "text": "[管理员群聊开关已同步]"}
        if raw.get("post_type") == "notice":
            notice = raw.get("notice_type")
            gid = signed_conversation(key)
            if notice in {"group_recall", "friend_recall"}:
                self.db.mark_message_recalled(group_id=gid, msg_id=str(raw["message_id"]),
                    recall_operator_id=raw.get("operator_id") or raw.get("user_id"),
                    recalled_at=unix_to_utc_naive(raw.get("time")))
                return {"eventId": event_id, "key": key, "kind": "recall", "messageId": str(raw["message_id"]),
                        "userId": str(raw.get("user_id") or raw.get("operator_id") or "0"), "text": "[消息已撤回]"}
            if notice == "notify" and raw.get("sub_type") == "poke":
                return {"eventId": event_id, "key": key, "kind": "poke", "userId": str(raw.get("user_id") or "0"),
                        "targetId": str(raw.get("target_id") or "0"), "text": "[拍一拍]", "poke": True}
            if notice == "group_ban":
                sub_type = str(raw.get("sub_type") or "ban")
                target_id = str(raw.get("user_id") or "0")
                bot_id = str(raw.get("self_id") or self.config.BOT_USER_ID or "")
                is_self = bool(bot_id and target_id == bot_id)
                duration = int(raw.get("duration", 0))
                operator_id = str(raw.get("operator_id") or "0")
                text = (f"[自己被禁言 {duration} 秒]" if duration > 0 else "[自己解除禁言]") if is_self else (f"[群友被禁言 {duration} 秒]" if duration > 0 else "[群友解除禁言]")
                payload = {"eventId": event_id, "key": key, "kind": "ban", "subType": sub_type,
                           "userId": target_id, "isSelf": is_self, "duration": duration,
                           "operatorId": operator_id, "text": text}
                if is_self:
                    system_notice = normalize_notice(raw, event_id, self.config.BOT_USER_ID)
                    if system_notice:
                        return {**system_notice, **payload, "isSystemEvent": True,
                                "observeOnly": key.startswith("group:") and raw.get("_yunying_chat_mode") is False}
                return payload
            system_notice = normalize_notice(raw, event_id, self.config.BOT_USER_ID)
            if system_notice:
                system_notice["observeOnly"] = key.startswith("group:") and raw.get("_yunying_chat_mode") is False
                return {**system_notice, "key": key}
            return None
        parsed = parse_message(raw)
        if not parsed:
            return None
        # A crash after raw history commit and before journal.prepare must still reach DSH.
        # Reuse committed media/text rather than downloading or transcribing the same message again.
        stored = self.db.get_message_by_msg_id(parsed["group_id"], parsed["msg_id"])
        if not stored:
            parsed = await persist_message(self, raw)
            if not parsed:
                return None
            stored = self.db.get_message_by_msg_id(parsed["group_id"], parsed["msg_id"])
        view = message_view(stored)
        sender = raw.get("sender") or {}
        bot_id = str(raw.get("self_id") or self.config.BOT_USER_ID)
        ats = [str(item.get("qq")) for item in parsed.get("message_structure", []) if item.get("type") == "at"]
        reply = self.db.get_message_by_msg_id(parsed["group_id"], parsed["reply_to_msg_id"]) if parsed.get("reply_to_msg_id") else None
        command = {} if parsed.get("is_self") or not self.authorized(key) else await self.command(raw, parsed, key, event_id)
        return {**view, **command, "eventId": event_id, "key": key, "kind": "message",
                "sender": str(sender.get("card") or sender.get("nickname") or parsed["sender_id"])[:120],
                "atBot": bot_id in ats, "replyToBot": bool(reply and reply.get("is_self")),
                "time": int(raw.get("time") or 0) * 1000,
                "observeOnly": key.startswith("group:") and raw.get("_yunying_chat_mode") is False
                    and not (bot_id in ats or bool(reply and reply.get("is_self")))}

    async def record(self):
        while not self._stopping:
            row = self.journal.pending_recording()
            if not row:
                self._journal_signal.clear()
                try:
                    await asyncio.wait_for(self._journal_signal.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                payload = decode(row["prepared_event"]) if row["prepared_event"] else await self.prepare(row)
                raw = decode(row["raw_event"])
                if payload and raw.get("_yunying_chat_mode") is False and not (payload.get("atBot") or payload.get("replyToBot")):
                    payload = {**payload, "observeOnly": True}
                self.journal.prepare(row["event_id"], payload)
                self._delivery_signal.set()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.journal.recording_retry(row["event_id"], row["recording_attempts"], error)
                logger.warning("Mechanical recording deferred: %s", type(error).__name__)

    async def deliver(self):
        while not self._stopping:
            row = self.journal.pending()
            if not row:
                self._delivery_signal.clear()
                try:
                    await asyncio.wait_for(self._delivery_signal.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                payload = decode(row["prepared_event"])
                if payload is not None and self.authorized(row["conversation_key"]):
                    gid = signed_conversation(row["conversation_key"])
                    if gid > 0:
                        payload = {**payload, **self.group_policy(gid)}
                    async with self.http.post(self.settings.dsh_url + "/events", json=payload,
                            headers={"Authorization": "Bearer " + self.settings.internal_secret},
                            timeout=ClientTimeout(total=30)) as response:
                        result = await response.json()
                        if response.status != 200 or result.get("ok") is not True:
                            raise RuntimeError("DSH durable event acceptance failed")
                self.journal.finish(row["event_id"])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.journal.retry(row["event_id"], row["attempts"], error)
                logger.warning("DSH delivery deferred: %s", type(error).__name__)

    async def send(self, data, owner_command=False):
        key = data["key"]
        if not self.authorized(key):
            raise web.HTTPForbidden(text="conversation denied")
        gid = signed_conversation(key)
        message = data.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > (2000 if owner_command else 500):
            raise web.HTTPBadRequest(text="invalid text")
        request_id = data.get("requestId", "")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise web.HTTPBadRequest(text="invalid send identity")
        reply = data.get("replyToMessageId")
        if reply is not None and not re.fullmatch(r"-?[1-9][0-9]{0,19}", str(reply)):
            raise web.HTTPBadRequest(text="invalid reply id")
        if reply and not self.db.get_message_by_msg_id(gid, str(reply)):
            raise web.HTTPForbidden(text="reply is outside conversation")
        at_user = data.get("atUserId")
        if at_user is not None:
            if gid < 0 or not re.fullmatch(r"[1-9][0-9]{0,19}", str(at_user)):
                raise web.HTTPBadRequest(text="invalid mention target")
            with self.db.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM message_logs WHERE group_id=%s AND sender_id=%s LIMIT 1", (gid, str(at_user)))
                if not cur.fetchone():
                    raise web.HTTPForbidden(text="mention is outside conversation")
        segments = ([{"type": "reply", "data": {"id": str(reply)}}] if reply else []) + ([{"type": "at", "data": {"qq": str(at_user)}}] if at_user else []) + [{"type": "text", "data": {"text": message}}]
        if not owner_command and not self.speaking_allowed(key, data.get("triggerEventId")):
            return {"ok": False, "error": "当前发言权限已关闭或呼叫已结束"}

        async def prepared():
            return {"segments": segments, "text": message, "reply": reply}

        return await self._deliver(key, gid, request_id, {"message": message, "reply": reply, "at": at_user}, prepared)

    async def _deliver(self, key, gid, request_id, journal_payload, provider):
        """Durable send core shared by text and artifact delivery.

        ``begin_send`` runs before any bytes are fetched so a repeated call
        returns the recorded outcome instead of sending or uploading twice.
        """
        async with self._send_lock:
            fresh, prior = self.journal.begin_send(request_id, key, journal_payload)
            if not fresh:
                return prior
            try:
                prepared = await provider()
            except Exception:
                result = {"ok": False, "status": "failed", "error": "附件不可用"}
                self.journal.finish_send(request_id, "failed", result)
                return result
            segments = prepared["segments"]
            message = prepared["text"]
            attachment = prepared.get("attachment")
            reply = prepared.get("reply")
            try:
                action = "send_group_msg" if gid > 0 else "send_private_msg"
                target = {"group_id": gid} if gid > 0 else {"user_id": -gid}
                async with self.http.post(self.config.ONEBOT_HTTP_URL.rstrip("/") + "/" + action,
                    json={**target, "message": segments, "auto_escape": True},
                    headers={"Authorization": "Bearer " + self.config.ONEBOT_HTTP_TOKEN},
                    timeout=ClientTimeout(total=15)) as response:
                    result = await response.json()
                    retcode = result.get("retcode")
                    is_mute = retcode == 120 or "result=120" in str(result)
                    if response.status != 200 or result.get("status") != "ok" or retcode != 0:
                        err_msg = "群内已被禁言" if is_mute else "OneBot 拒绝发送"
                        result = {"ok": False, "status": "failed", "error": err_msg, "muted": is_mute}
                        self.journal.finish_send(request_id, "failed", result)
                        return result
                message_id = str((result.get("data") or {}).get("message_id") or "")
                result = {"ok": bool(message_id), "status": "sent" if message_id else "unknown", "message_id": message_id or None}
                # Persist remote outcome first; DB history failure must never cause an automatic resend.
                self.journal.finish_send(request_id, result["status"], result)
                if message_id:
                    try:
                        self.db.insert_message(group_id=gid, sender_id=self.config.BOT_USER_ID,
                            message_content=message, plain_text_content=message,
                            message_structure=encode(segments),
                            attachments_json=encode([attachment]) if attachment else None,
                            reply_to_msg_id=str(reply) if reply else None,
                            msg_id=message_id, is_self=True, message_source="yunying_dsh")
                    except Exception:
                        logger.error("Sent message history awaits OneBot echo compensation")
                return result
            except (asyncio.CancelledError, Exception) as error:
                # A timeout after remote acceptance is not proof of failure. No retry across restart.
                self.journal.finish_send(request_id, "unknown", {"ok": False, "status": "unknown", "error": "发送结果不确定；不得自动重发"})
                if isinstance(error, asyncio.CancelledError):
                    raise
                return {"ok": False, "status": "unknown", "error": "发送结果不确定；不得自动重发"}

    async def terminal_send(self, data):
        key = data["key"]
        gid = signed_conversation(key)
        if not self.speaking_allowed(key, data.get("triggerEventId")):
            return {"ok": False, "error": "当前发言权限已关闭或呼叫已结束"}
        return await self.terminal.send(data, gid=gid, deliver=self._deliver)

    async def terminal_call(self, path, data):
        if self.terminal is None:
            raise web.HTTPServiceUnavailable(text="terminal broker is not configured")
        if path == "/terminal/open":
            return await self.terminal.open(data)
        if path == "/terminal/send":
            return await self.terminal_send(data)
        return await self.terminal.operate(path, data)

    async def api(self, request):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + self.settings.internal_secret):
            raise web.HTTPUnauthorized()
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        data = await request.json()
        key = data.get("key", "")
        if request.path == "/ai-records":
            return web.json_response(await self.ai_record(data))
        if request.path == "/identity":
            return web.json_response(await self.identity(data))
        if request.path == "/authorized":
            try:
                allowed_key = self.authorized(str(data.get("key", "")))
            except Exception:
                allowed_key = False
            return web.json_response({"ok": True, "allowed": allowed_key})
        if not self.authorized(key):
            raise web.HTTPForbidden()
        if request.path == "/account-status":
            return web.json_response(await self.account_status())
        if request.path == "/notifications":
            try:
                limit = int(data.get("limit", 10))
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text="invalid notification limit")
            return web.json_response(await self.notifications(key, limit))
        if request.path == "/group-info":
            return web.json_response(await self.current_group_info(key))
        if request.path == "/member-info":
            return web.json_response(await self.current_member_info(key, data.get("userId")))
        if request.path == "/bot-permissions":
            return web.json_response(await self.bot_permissions(key))
        gid = signed_conversation(key)
        if request.path.startswith("/terminal/"):
            return web.json_response(await self.terminal_call(request.path, data))
        if request.path == "/send":
            return web.json_response(await self.send(data))
        if request.path == "/history":
            limit = min(100, max(1, int(data.get("limit", 20))))
            offset = min(10000, max(0, int(data.get("offset", 0))))
            with self.db.conn.cursor() as cur:
                cur.execute("SELECT * FROM message_logs WHERE group_id=%s ORDER BY id DESC LIMIT %s OFFSET %s", (gid, limit, offset))
                rows = cur.fetchall()[::-1]
            return web.json_response({"ok": True, "messages": [message_view(row) for row in rows]})
        if request.path in {"/detail", "/images"}:
            row = self.db.get_message_by_msg_id(gid, str(data.get("messageId", "")))
            if not row:
                raise web.HTTPNotFound()
            if request.path == "/images":
                images = await asyncio.to_thread(row_images, row, self.storage, self.config.MESSAGE_ROOT, 3)
                return web.json_response({"ok": True, "images": images, "isRecalled": bool(row.get("is_recalled"))})
            return web.json_response({"ok": True, "message": message_view(row)})
        if request.path == "/poke":
            if not self.speaking_allowed(key, data.get("triggerEventId")):
                return web.json_response({"ok": False, "error": "当前发言权限已关闭或呼叫已结束"})
            # Fixed capability, never an arbitrary OneBot action supplied by the model.
            uid = str(data.get("userId", ""))
            if not re.fullmatch(r"[1-9][0-9]{0,19}", uid):
                raise web.HTTPBadRequest()
            if gid < 0 and uid != str(-gid):
                raise web.HTTPForbidden()
            if gid > 0:
                with self.db.conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM message_logs WHERE group_id=%s AND sender_id=%s LIMIT 1", (gid, uid))
                    if not cur.fetchone():
                        raise web.HTTPForbidden()
            fresh, prior = self.journal.begin_send(data["requestId"], key, {"poke": uid})
            if not fresh:
                return web.json_response(prior)
            try:
                async with self.http.post(self.config.ONEBOT_HTTP_URL.rstrip("/") + "/send_poke", json={"user_id": int(uid), **({"group_id": gid} if gid > 0 else {})}, headers={"Authorization": "Bearer " + self.config.ONEBOT_HTTP_TOKEN}, timeout=ClientTimeout(total=15)) as response:
                    body = await response.json()
                    ok = response.status == 200 and body.get("status") == "ok" and body.get("retcode") == 0
                    result = {"ok": ok}
                    self.journal.finish_send(data["requestId"], "sent" if ok else "failed", result)
                    return web.json_response(result)
            except Exception:
                self.journal.finish_send(data["requestId"], "unknown", {"ok": False, "status": "unknown"})
                return web.json_response({"ok": False, "status": "unknown"})
        if request.path == "/reaction":
            if not self.speaking_allowed(key, data.get("triggerEventId")):
                return web.json_response({"ok": False, "error": "当前发言权限已关闭或呼叫已结束"})
            if gid <= 0:
                return web.json_response({"ok": False, "error": "表情回应仅支持群聊会话"})
            msg_id = str(data.get("messageId", ""))
            if not msg_id or not self.db.get_message_by_msg_id(gid, msg_id):
                raise web.HTTPNotFound()
            emoji_id = str(data.get("emojiId", ""))
            if not re.fullmatch(r"[0-9]{1,10}", emoji_id):
                raise web.HTTPBadRequest()
            fresh, prior = self.journal.begin_send(data["requestId"], key, {"reaction": msg_id, "emoji_id": emoji_id})
            if not fresh:
                return web.json_response(prior)
            try:
                payload = {"group_id": gid, "message_id": int(msg_id) if msg_id.lstrip("-").isdigit() else msg_id, "emoji_id": emoji_id}
                async with self.http.post(self.config.ONEBOT_HTTP_URL.rstrip("/") + "/set_msg_emoji_like",
                                         json=payload,
                                         headers={"Authorization": "Bearer " + self.config.ONEBOT_HTTP_TOKEN},
                                         timeout=ClientTimeout(total=15)) as response:
                    body = await response.json()
                    ok = response.status == 200 and body.get("status") == "ok" and body.get("retcode") == 0
                    result = {"ok": ok}
                    self.journal.finish_send(data["requestId"], "sent" if ok else "failed", result)
                    return web.json_response(result)
            except Exception:
                self.journal.finish_send(data["requestId"], "unknown", {"ok": False, "status": "unknown"})
                return web.json_response({"ok": False, "status": "unknown"})
        raise web.HTTPNotFound()

    async def identity(self, data):
        """Resolve QQ(s) to every QQ of their IGNG account for cross-group memory reads."""
        values = data.get("qqs")
        if not isinstance(values, list):
            values = [data.get("qq")]
        base = []
        for value in values:
            text = str(value)
            if re.fullmatch(r"[1-9][0-9]{0,19}", text) and text not in base:
                base.append(text)
        base = base[:50]
        expanded = set(base)
        accounts = set()
        for qq in base:
            account = await asyncio.to_thread(self.db.resolve_bound_igng_account_id, qq)
            if account:
                accounts.add(account)
        for account in accounts:
            for qq in await asyncio.to_thread(self.db.get_account_qqs, account):
                if re.fullmatch(r"[1-9][0-9]{0,19}", str(qq)):
                    expanded.add(str(qq))
        ordered = sorted(expanded, key=int)[:200]
        return {"ok": True, "qqs": ordered}

    async def ai_record(self, data):
        record_id = data["recordId"]
        with self.conn.cursor() as cur:
            cur.execute("SELECT payload FROM yunying_ai_records WHERE record_id=%s", (record_id,))
            source = cur.fetchone()
            if not source:
                raise web.HTTPNotFound()
        payload = decode(source["payload"])
        if payload.get("task_key"):
            return {"ok": bool(await mirror_native_record(record_id, payload)), "callLogId": None}
        # Already-linked V4 baseline records retain their original website job IDs.
        # No new call_logs rows are ever produced by V4, including replay/retry.
        with self.conn.cursor() as cur:
            cur.execute("SELECT call_log_id FROM yunying_ai_records WHERE record_id=%s", (record_id,))
            call_id = cur.fetchone()["call_log_id"]
        if call_id:
            mirrored = await mirror_call_to_site(call_log_id=call_id, durable=True, **{k: v for k, v in payload.items() if k in {
                "group_id", "sender_id", "sender_name", "message_text", "call_type", "model", "system_prompt", "user_prompt",
                "response_content", "token_usage", "duration_ms", "success", "error_message", "provider"}})
        else:
            mirrored = await mirror_native_record(record_id, historical_record(record_id, payload))
        return {"ok": bool(mirrored), "callLogId": call_id}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.db.connect()
        self.db.init_message_tables()
        self.db.init_group_configs_table()
        self.conn = connect(self.config)
        migrate(self.conn)
        with self.conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(CONCAT('yunying-onebot:',DATABASE()),0) AS owned")
            if cur.fetchone()["owned"] != 1:
                raise RuntimeError("another V4 OneBot consumer owns this database")
        self.journal = Journal(self.conn)
        self.storage.check_available()
        self.http = ClientSession()
        if self.settings.broker_url:
            self.broker = BrokerClient(self.settings.broker_url, self.settings.broker_secret)
            self.terminal = TerminalGateway(self.broker, authorize=self.authorized, db=self.db,
                                            storage=self.storage, config=self.config)
        app = web.Application(client_max_size=4 * 1024 * 1024)
        async def health(_request):
            try:
                self.conn.ping(reconnect=False)
                return web.json_response({"ok": not self._stopping, "version": "4.0.0"})
            except Exception:
                return web.json_response({"ok": False}, status=503)
        app.router.add_get("/health", health)
        app.router.add_post("/terminal/{action}", self.api)
        app.router.add_post("/{capability}", self.api)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        await web.TCPSite(runner, self.settings.host, self.settings.port).start()
        stop = asyncio.Event()
        await self.sync_group_modes()
        recorder = asyncio.create_task(self.record())
        worker = asyncio.create_task(self.deliver())
        config_worker = asyncio.create_task(self.monitor_group_modes())
        def delivery_done(task):
            if not self._stopping:
                failure = 'Cancelled' if task.cancelled() else type(task.exception()).__name__
                logger.error('Durable delivery worker stopped: %s', failure)
                stop.set()
        recorder.add_done_callback(delivery_done)
        worker.add_done_callback(delivery_done)
        config_worker.add_done_callback(delivery_done)
        notifier = None
        if self.config.IGNG_SITE_DB_HOST:
            notifier = McTicketNotifier(self.config, db=self.db)
            await notifier.start()
        self.client = OneBotClient(self.config, self.callback, recall_callback=self.callback, notice_callback=self.callback)
        thread = threading.Thread(target=self.client.start, name="yunying-onebot", daemon=True)
        thread.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, stop.set)
        logger.info("YunYing V4 infrastructure ready; group/private access uses explicit allowlists")
        try:
            await stop.wait()
        finally:
            self._stopping = True
            self.client.stop()
            recorder.cancel()
            worker.cancel()
            config_worker.cancel()
            await asyncio.gather(recorder, worker, config_worker, return_exceptions=True)
            if notifier:
                await notifier.stop()
            if self.terminal:
                await self.terminal.close()
            await runner.cleanup()
            await self.http.close()
            await close_call_log_pool()
            self.conn.close()
            self.db.conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(Infrastructure().run())
