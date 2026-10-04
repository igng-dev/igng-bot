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
from igngbot_v3.config import Config
from igngbot_v3.db import DBHandler
from igngbot_v3.storage import StorageHandler
from igngbot_v3.media_text import MediaTextExtractor
from igngbot_v3.message_ingest import persist_message
from igngbot_v3.message_parser import parse_message
from igngbot_v3.onebot_client import OneBotClient
from igngbot_v3.mc_ticket_notifications import McTicketNotifier
from igngbot_v3.timeutil import unix_to_utc_naive
from igngbot_v3.call_log_db import ensure_call_logs_table, insert_call_log, mirror_call_to_site, close_call_log_pool
from .journal import Journal, decode, encode
from .migrate import connect, migrate
from .settings import Settings, signed_conversation
from .views import message_view, row_images, safe_structure

logger = logging.getLogger("yunying.infrastructure")


class Infrastructure:
    def __init__(self, config=None, settings=None):
        self.config = config or Config()
        self.settings = settings or Settings.from_env()
        self.db = DBHandler(self.config)
        self.storage = StorageHandler(self.config)
        self.media_text = MediaTextExtractor(self.config)
        self.conn = None
        self.journal = None
        self.http = None
        self.loop = None
        self.client = None
        self._send_lock = asyncio.Lock()
        self._stopping = False
        self._journal_signal = asyncio.Event()
        self._group_modes = {}

    async def enqueue(self, raw):
        key, _ = self.journal.enqueue(raw)
        # Durability precedes any slow media download or DSH contact.
        self._journal_signal.set()
        return key

    def callback(self, raw):
        if self._stopping:
            return
        # Called on the WebSocket thread; acknowledge only after durable DB acceptance.
        asyncio.run_coroutine_threadsafe(self.enqueue(raw), self.loop).result()
        if raw.get("self_id") and not self.config.BOT_USER_ID:
            self.config.BOT_USER_ID = int(raw["self_id"])

    async def command(self, raw, parsed, key, event_id):
        text = (parsed.get("message_content") or "").strip()
        sender = parsed["sender_id"]
        central_admin = lambda: self.db.is_bot_admin(sender)
        message = None
        handled = False
        pause = None
        consent = re.fullmatch(r"/记忆共享\s+(开启|关闭|on|off)", text, re.I)
        if consent:
            enabled = consent.group(1).lower() in {"开启", "on"}
            self.journal.set_sharing(sender, enabled)
            message = "个人记忆跨群共享已开启。只有你本人提供的信息可写入共享记忆。" if enabled else "个人记忆跨群共享已关闭；其他会话将无法读取你的共享记忆。"
            handled = True
        elif text in {"/help", "/帮助"}:
            message = "/云萤暂停、/云萤继续：群管理员或 bot 管理员控制发言。\n/用户组 [pro|plus IGNG用户ID]：沿用网站权限。\n/记忆共享 开启|关闭：本人选择个人记忆是否跨群可见。\n直接 @ 或回复云萤即可聊天。"
            handled = True
        elif text in {"/云萤暂停", "/云萤继续"} and key.startswith("group:"):
            handled = True
            if parsed.get("sender_role") in {"owner", "admin"} or central_admin():
                gid = signed_conversation(key)
                pause = text == "/云萤暂停"
                with self.db.conn.cursor() as cur:
                    cur.execute("INSERT INTO group_configs (group_id,social_paused) VALUES (%s,%s) "
                                "ON DUPLICATE KEY UPDATE social_paused=VALUES(social_paused)", (gid, int(pause)))
                self.db.conn.commit()
                message = "云萤已暂停发言，消息仍正常保存。" if pause else "云萤已恢复发言。"
            else:
                message = "需要群管理员或 bot 管理员权限。"
        elif text == "/聊天模式" and key.startswith("group:"):
            handled = True
            message = "V4 由云萤自主决定参与群聊。管理员可用 /云萤暂停 或 /云萤继续 控制发言。"
        else:
            match = re.fullmatch(r"/用户组(?:\s+(pro|plus)\s+#?(\d+))?", text, re.I)
            if match:
                handled = True
                if match.group(1):
                    if central_admin():
                        self.db.set_user_group(match.group(2), match.group(1).lower())
                        message = f"IGNG 用户 {match.group(2)} 已设置为 {match.group(1).lower()} 组。"
                    else:
                        message = "只有 bot 管理员可以设置用户组。"
                else:
                    message = f"当前用户组：{self.db.get_user_group(sender)}"
        if message:
            await self.send({"key": key, "requestId": f"command:{event_id}", "message": message}, owner_command=True)
        return {"commandHandled": handled, **({"pause": pause} if pause is not None else {})}

    def group_enabled(self, gid):
        # V3 is_chat_mode only governed unsolicited chat; it is not a pause permission.
        # This lease connection is autocommit. The legacy raw-history connection
        # uses repeatable-read transactions and would cache a website toggle.
        with self.conn.cursor() as cur:
            cur.execute("SELECT social_paused FROM group_configs WHERE group_id=%s", (gid,))
            row = cur.fetchone()
            return bool(row and not row.get("social_paused"))

    def conversation_paused(self, key):
        gid = signed_conversation(key)
        if gid > 0 and not self.group_enabled(gid):
            return True
        with self.conn.cursor() as cur:
            cur.execute("SELECT paused FROM yunying_sessions WHERE conversation_key=%s", (key,))
            row = cur.fetchone()
            return bool(row and row["paused"])

    async def sync_group_modes(self):
        for group in sorted(self.settings.groups):
            enabled = self.group_enabled(int(group))
            if self._group_modes.get(group) == enabled:
                continue
            # Trusted pause configuration shares the durable per-conversation FIFO.
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
        command = {} if parsed.get("is_self") or not self.settings.allowed(key) else await self.command(raw, parsed, key, event_id)
        return {**view, **command, "eventId": event_id, "key": key, "kind": "message",
                "sender": str(sender.get("card") or sender.get("nickname") or parsed["sender_id"])[:120],
                "atBot": bot_id in ats, "replyToBot": bool(reply and reply.get("is_self")),
                "time": int(raw.get("time") or 0) * 1000}

    async def deliver(self):
        while not self._stopping:
            row = self.journal.pending()
            if not row:
                self._journal_signal.clear()
                try:
                    await asyncio.wait_for(self._journal_signal.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                payload = decode(row["prepared_event"]) if row["prepared_event"] else await self.prepare(row)
                if payload is not None and not row["prepared_event"]:
                    self.journal.prepare(row["event_id"], payload)
                if payload is not None and self.settings.allowed(row["conversation_key"]):
                    gid = signed_conversation(row["conversation_key"])
                    if gid > 0:
                        # Prepared/retried events must not restore an obsolete pause permission.
                        payload = {**payload, "pause": not self.group_enabled(gid)}
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
                logger.warning("Ingress delivery deferred: %s", type(error).__name__)

    async def send(self, data, owner_command=False):
        key = data["key"]
        if not self.settings.allowed(key):
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
        async with self._send_lock:
            if not owner_command and self.conversation_paused(key):
                return {"ok": False, "error": "管理员已暂停本会话发言"}
            fresh, prior = self.journal.begin_send(request_id, key, {"message": message, "reply": reply, "at": at_user})
            if not fresh:
                return prior
            try:
                action = "send_group_msg" if gid > 0 else "send_private_msg"
                target = {"group_id": gid} if gid > 0 else {"user_id": -gid}
                async with self.http.post(self.config.ONEBOT_HTTP_URL.rstrip("/") + "/" + action,
                    json={**target, "message": segments, "auto_escape": True},
                    headers={"Authorization": "Bearer " + self.config.ONEBOT_HTTP_TOKEN},
                    timeout=ClientTimeout(total=15)) as response:
                    result = await response.json()
                    if response.status != 200 or result.get("status") != "ok" or result.get("retcode") != 0:
                        result = {"ok": False, "status": "failed", "error": "OneBot 拒绝发送"}
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
                            message_structure=encode(segments), attachments_json=None, reply_to_msg_id=str(reply) if reply else None,
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
        if not self.settings.allowed(key):
            raise web.HTTPForbidden()
        gid = signed_conversation(key)
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
            if self.conversation_paused(key):
                return web.json_response({"ok": False, "error": "管理员已暂停本会话发言"})
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
        raise web.HTTPNotFound()

    async def ai_record(self, data):
        record_id = data["recordId"]
        # Local history insert and journal link commit together, including retries after HTTP loss.
        self.conn.begin()
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT call_log_id,payload FROM yunying_ai_records WHERE record_id=%s FOR UPDATE", (record_id,))
                row = cur.fetchone()
                if not row:
                    raise web.HTTPNotFound()
                payload = decode(row["payload"])
                call_id = row["call_log_id"]
                if not call_id:
                    columns = ["group_id", "sender_id", "sender_name", "message_text", "call_type", "model", "system_prompt", "user_prompt", "response_content", "tool_calls", "token_usage", "duration_ms", "success", "error_message"]
                    values = [encode(payload.get(k)) if k in {"tool_calls", "token_usage"} else payload.get(k, "") for k in columns]
                    cur.execute("INSERT INTO call_logs (" + ",".join(columns) + ") VALUES (" + ",".join(["%s"] * len(columns)) + ")", values)
                    call_id = cur.lastrowid
                    cur.execute("UPDATE yunying_ai_records SET call_log_id=%s WHERE record_id=%s", (call_id, record_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        mirrored = await mirror_call_to_site(call_log_id=call_id, durable=True, **{k: v for k, v in payload.items() if k in {
            "group_id", "sender_id", "sender_name", "message_text", "call_type", "model", "system_prompt", "user_prompt",
            "response_content", "token_usage", "duration_ms", "success", "error_message", "provider"}})
        return {"ok": bool(mirrored), "callLogId": call_id}

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.db.connect()
        self.db.init_table()
        self.db.init_group_configs_table()
        self.conn = connect(self.config)
        migrate(self.conn)
        with self.conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(CONCAT('yunying-onebot:',DATABASE()),0) AS owned")
            if cur.fetchone()["owned"] != 1:
                raise RuntimeError("another V4 OneBot consumer owns this database")
        self.journal = Journal(self.conn)
        self.storage.check_available()
        await ensure_call_logs_table()
        self.http = ClientSession()
        app = web.Application(client_max_size=4 * 1024 * 1024)
        async def health(_request):
            try:
                self.conn.ping(reconnect=False)
                return web.json_response({"ok": not self._stopping, "version": "4.0.0"})
            except Exception:
                return web.json_response({"ok": False}, status=503)
        app.router.add_get("/health", health)
        app.router.add_post("/{capability}", self.api)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self.settings.host, self.settings.port).start()
        stop = asyncio.Event()
        await self.sync_group_modes()
        worker = asyncio.create_task(self.deliver())
        config_worker = asyncio.create_task(self.monitor_group_modes())
        def delivery_done(task):
            if not self._stopping:
                failure = 'Cancelled' if task.cancelled() else type(task.exception()).__name__
                logger.error('Durable delivery worker stopped: %s', failure)
                stop.set()
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
            worker.cancel()
            config_worker.cancel()
            await asyncio.gather(worker, config_worker, return_exceptions=True)
            if notifier:
                await notifier.stop()
            await runner.cleanup()
            await self.http.close()
            await close_call_log_pool()
            self.conn.close()
            self.db.conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(Infrastructure().run())
