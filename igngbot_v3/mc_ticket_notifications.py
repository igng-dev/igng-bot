import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomysql

from .config import Config
from .onebot_api import OUTBOUND_SOURCE_NOTIFICATION, send_group_text, send_private_text

logger = logging.getLogger(__name__)


TICKET_TYPE_LABELS = {
    "REPORT": "举报",
    "APPEAL": "申诉",
    "FEEDBACK": "反馈",
}

VIEW_GROUP_AUDIENCES = {
    "PUBLIC": ("ADMIN",),
    "mc.admin": ("ADMIN",),
    "mc.tech.admin": ("TECHADMIN",),
    "platform.superadmin": ("SUPERADMIN",),
}

NOTIFICATION_SCOPE_GROUPS = {
    "ADMIN": ("管理员", "mc.admin"),
    "TECHADMIN": ("技术管理员", "mc.tech.admin"),
    "SUPERADMIN": ("服主", "platform.superadmin"),
}


class MySqlNotificationStore:
    """Persist source cursors and per-destination delivery receipts."""

    def __init__(self, config):
        self.config = config
        self._pool = None

    async def start(self):
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=self.config.DB_HOST,
            port=self.config.DB_PORT,
            user=self.config.DB_USER,
            password=self.config.DB_PASSWORD,
            db=self.config.DB_NAME,
            charset="utf8mb4",
            ssl=Config.db_ssl_context(),
            autocommit=True,
            minsize=1,
            maxsize=2,
            init_command="SET time_zone = '+00:00'",
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mc_ticket_notification_state (
                        stream VARCHAR(32) NOT NULL PRIMARY KEY,
                        last_id BIGINT NOT NULL,
                        initialized_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                await cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mc_ticket_notification_deliveries (
                        event_key VARCHAR(96) NOT NULL,
                        destination_type VARCHAR(16) NOT NULL,
                        destination_id VARCHAR(64) NOT NULL,
                        sent_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (event_key, destination_type, destination_id),
                        KEY idx_mc_ticket_notification_sent_at (sent_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )

    async def close(self):
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None

    async def get_or_seed_cursor(self, stream, seed_id):
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    INSERT INTO mc_ticket_notification_state (stream, last_id)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE stream = VALUES(stream)
                    """,
                    (stream, int(seed_id)),
                )
                await cursor.execute(
                    "SELECT last_id FROM mc_ticket_notification_state WHERE stream = %s",
                    (stream,),
                )
                row = await cursor.fetchone()
        if not row:
            raise RuntimeError(f"notification cursor was not initialized: {stream}")
        return int(row["last_id"])

    async def set_cursor(self, stream, last_id):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO mc_ticket_notification_state (stream, last_id)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE last_id = GREATEST(last_id, VALUES(last_id))
                    """,
                    (stream, int(last_id)),
                )

    async def was_delivered(self, event_key, destination_type, destination_id):
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT 1 AS delivered
                    FROM mc_ticket_notification_deliveries
                    WHERE event_key = %s
                      AND destination_type = %s
                      AND destination_id = %s
                    LIMIT 1
                    """,
                    (event_key, destination_type, str(destination_id)),
                )
                return await cursor.fetchone() is not None

    async def mark_delivered(self, event_key, destination_type, destination_id):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT IGNORE INTO mc_ticket_notification_deliveries
                        (event_key, destination_type, destination_id)
                    VALUES (%s, %s, %s)
                    """,
                    (event_key, destination_type, str(destination_id)),
                )


class McTicketNotifier:
    """Poll central ticket events and deliver fail-closed, visibility-scoped alerts."""

    def __init__(self, config, state_store=None, db=None):
        self.config = config
        self.db = db
        self._pool = None
        self._state_store = state_store
        self._owns_state_store = state_store is None
        self._task = None
        self._daily_task = None
        self._last_ticket_id = 0
        self._last_message_id = 0
        self._timezone = self._resolve_timezone(self.config.MC_TICKET_TIMEZONE)

    @staticmethod
    def _resolve_timezone(name):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            if name == "Asia/Shanghai":
                return timezone(timedelta(hours=8), "CST")
            raise

    async def start(self):
        if self._task is not None and not self._task.done():
            return

        self._pool = await aiomysql.create_pool(
            host=self.config.IGNG_SITE_DB_HOST,
            port=self.config.IGNG_SITE_DB_PORT,
            user=self.config.IGNG_SITE_DB_USER,
            password=self.config.IGNG_SITE_DB_PASSWORD,
            db=self.config.IGNG_SITE_DB_NAME,
            charset="utf8mb4",
            ssl=Config.db_ssl_context(),
            autocommit=True,
            minsize=1,
            maxsize=3,
            init_command="SET time_zone = '+00:00'",
        )
        if self._state_store is None:
            self._state_store = MySqlNotificationStore(self.config)

        try:
            if hasattr(self._state_store, "start"):
                await self._state_store.start()
            ticket_seed, reply_seed = await self._get_current_cursors()
            self._last_ticket_id = await self._state_store.get_or_seed_cursor(
                "ticket", ticket_seed
            )
            self._last_message_id = await self._state_store.get_or_seed_cursor(
                "reply", reply_seed
            )
        except Exception:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            if self._owns_state_store and self._state_store is not None:
                await self._state_store.close()
                self._state_store = None
            raise

        self._task = asyncio.create_task(self._run(), name="mc-ticket-notifier")
        self._daily_task = asyncio.create_task(
            self._run_daily_reminder(),
            name="mc-ticket-daily-reminder",
        )
        logger.info(
            "MC ticket notifications enabled: admin_group=%s, tech_group=%s, "
            "ticket_id=%s, message_id=%s",
            self.config.MC_TICKET_NOTIFICATION_GROUP,
            self.config.MC_TICKET_TECH_NOTIFICATION_GROUP,
            self._last_ticket_id,
            self._last_message_id,
        )
        logger.info(
            "MC ticket daily reminder scheduled at %02d:00 (%s)",
            self.config.MC_TICKET_DAILY_REMINDER_HOUR,
            self.config.MC_TICKET_TIMEZONE,
        )

    async def close(self):
        tasks = [task for task in (self._task, self._daily_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._daily_task = None
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        if self._owns_state_store and self._state_store is not None:
            await self._state_store.close()
            self._state_store = None

    async def _get_current_cursors(self):
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT COALESCE(MAX(id), 0) AS id FROM mc_tickets")
                ticket_id = int((await cursor.fetchone())["id"])
                await cursor.execute(
                    """
                    SELECT COALESCE(MAX(id), 0) AS id
                    FROM mc_ticket_messages
                    WHERE kind = 'REPLY'
                    """
                )
                message_id = int((await cursor.fetchone())["id"])
        return ticket_id, message_id

    async def _run(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MC ticket notification poll failed")
            await asyncio.sleep(self.config.MC_TICKET_POLL_INTERVAL)

    async def _run_daily_reminder(self):
        while True:
            now = datetime.now(self._timezone)
            target = now.replace(
                hour=self.config.MC_TICKET_DAILY_REMINDER_HOUR,
                minute=0,
                second=0,
                microsecond=0,
            )
            if target <= now:
                target += timedelta(days=1)

            await asyncio.sleep((target - now).total_seconds())
            try:
                await self._send_daily_reminder()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MC ticket daily reminder failed")

    async def _send_daily_reminder(self):
        tickets = await self._fetch(
            """
            SELECT t.id, t.title, t.type, t.server_scope, t.source_server_name,
                   t.creator_user_id, t.creator_name, t.created_at,
                   (
                     SELECT p.group_code
                     FROM mc_ticket_access_policies p
                     WHERE p.ticket_id = t.id AND p.action = 'VIEW'
                   ) AS view_group_code
            FROM mc_tickets t
            WHERE t.status = 'OPEN'
            ORDER BY t.created_at ASC, t.id ASC
            """,
            (),
        )
        tickets_by_scope = {"ADMIN": [], "TECHADMIN": [], "SUPERADMIN": []}
        for ticket in tickets:
            for scope in self._notification_scopes(ticket):
                tickets_by_scope[scope].append(ticket)

        reminder_date = datetime.now(self._timezone).date().isoformat()
        for scope, scoped_tickets in tickets_by_scope.items():
            if not scoped_tickets:
                continue
            header_lines = [
                f"【MC工单提醒】当前可见未结单工单（{len(scoped_tickets)} 条）",
                self._format_channel_scope(scope),
                "以下工单均为该权限组当前可见：",
            ]
            ticket_lines = [
                self._format_daily_ticket(ticket) for ticket in scoped_tickets
            ]
            messages = self._split_daily_messages(header_lines, ticket_lines)
            for index, message in enumerate(messages):
                await self._send_visibility(
                    scope,
                    message,
                    message,
                    event_key=f"daily:{reminder_date}:{scope}:{index}",
                )

    async def _poll_once(self):
        tickets = await self._fetch(
            """
            SELECT t.id, t.title, t.type, t.server_scope, t.source_server_name,
                   t.status, t.creator_user_id, t.creator_name, t.created_at,
                   (
                     SELECT p.group_code
                     FROM mc_ticket_access_policies p
                     WHERE p.ticket_id = t.id AND p.action = 'VIEW'
                   ) AS view_group_code,
                   (
                     SELECT p.group_code
                     FROM mc_ticket_access_policies p
                     WHERE p.ticket_id = t.id AND p.action = 'PARTICIPATE'
                   ) AS participate_group_code,
                   (
                     SELECT p.group_code
                     FROM mc_ticket_access_policies p
                     WHERE p.ticket_id = t.id AND p.action = 'MANAGE'
                   ) AS manage_group_code,
                   u.username AS creator_username, u.nickname AS creator_nickname
            FROM mc_tickets t
            LEFT JOIN users u ON u.id = t.creator_user_id
            WHERE t.id > %s
            ORDER BY t.id ASC
            LIMIT 20
            """,
            (self._last_ticket_id,),
        )
        for ticket in tickets:
            for scope in self._notification_scopes(ticket):
                message = self._format_new_ticket(ticket, scope)
                await self._send_visibility(
                    scope,
                    message,
                    message,
                    event_key=f"ticket:{int(ticket['id'])}",
                )
            await self._advance_cursor("ticket", int(ticket["id"]))

        replies = await self._fetch(
            """
            SELECT m.id, m.ticket_id, m.author_name, m.content, m.created_at,
                   t.title, t.type, t.server_scope, t.source_server_name,
                   t.status,
                   (
                     SELECT p.group_code
                     FROM mc_ticket_access_policies p
                     WHERE p.ticket_id = t.id AND p.action = 'VIEW'
                   ) AS view_group_code
            FROM mc_ticket_messages m
            INNER JOIN mc_tickets t ON t.id = m.ticket_id
            WHERE m.id > %s
              AND m.kind = 'REPLY'
            ORDER BY m.id ASC
            LIMIT 20
            """,
            (self._last_message_id,),
        )
        for reply in replies:
            for scope in self._notification_scopes(reply):
                message = self._format_new_reply(reply, scope)
                await self._send_visibility(
                    scope,
                    message,
                    message,
                    event_key=f"reply:{int(reply['id'])}",
                )
            await self._advance_cursor("reply", int(reply["id"]))

    async def _advance_cursor(self, stream, last_id):
        await self._state_store.set_cursor(stream, last_id)
        if stream == "ticket":
            self._last_ticket_id = int(last_id)
        elif stream == "reply":
            self._last_message_id = int(last_id)
        else:
            raise RuntimeError(f"unknown MC ticket notification stream: {stream!r}")

    async def _send_visibility(self, scope, group_message, private_message, event_key):
        if scope == "ADMIN":
            await self._deliver_target(
                event_key,
                "GROUP",
                self.config.MC_TICKET_NOTIFICATION_GROUP,
                group_message,
            )
            return

        if scope == "TECHADMIN":
            await self._deliver_target(
                event_key,
                "GROUP",
                self.config.MC_TICKET_TECH_NOTIFICATION_GROUP,
                group_message,
            )
            return

        if scope != "SUPERADMIN":
            raise RuntimeError(f"unknown MC ticket notification scope: {scope!r}")

        qq_numbers = await self._get_superadmin_qqs()
        if not qq_numbers:
            raise RuntimeError(
                "no active superadmin QQ bindings available for a superadmin-visible ticket"
            )
        failures = []
        for qq_number in qq_numbers:
            try:
                await self._deliver_target(
                    event_key,
                    "PRIVATE",
                    qq_number,
                    private_message,
                )
            except RuntimeError:
                failures.append(qq_number)
        if failures:
            raise RuntimeError(
                f"failed to send MC ticket private notification to {len(failures)} target(s)"
            )

    async def _deliver_target(
        self,
        event_key,
        destination_type,
        destination_id,
        message,
    ):
        if not message:
            raise RuntimeError("refusing to send an empty MC ticket notification")
        destination_id = str(destination_id)
        if await self._state_store.was_delivered(
            event_key, destination_type, destination_id
        ):
            return

        if destination_type == "GROUP":
            if self.db is None:
                sent = await send_group_text(self.config, destination_id, message)
            else:
                sent = await send_group_text(
                    self.config,
                    destination_id,
                    message,
                    db=self.db,
                    message_source=OUTBOUND_SOURCE_NOTIFICATION,
                )
        elif destination_type == "PRIVATE":
            if self.db is None:
                sent = await send_private_text(self.config, destination_id, message)
            else:
                sent = await send_private_text(
                    self.config,
                    destination_id,
                    message,
                    db=self.db,
                    message_source=OUTBOUND_SOURCE_NOTIFICATION,
                )
        else:
            raise RuntimeError(
                f"unknown MC ticket notification destination: {destination_type!r}"
            )
        if not sent:
            raise RuntimeError(
                f"failed to send MC ticket notification to {destination_type.lower()} target"
            )
        await self._state_store.mark_delivered(
            event_key, destination_type, destination_id
        )

    async def _get_superadmin_qqs(self):
        memberships = await self._fetch(
            """
            SELECT upg.user_id, upg.group_id
            FROM user_permission_groups upg
            INNER JOIN users u ON u.id = upg.user_id
            INNER JOIN permission_groups pg ON pg.id = upg.group_id
            WHERE u.status <> 'BANNED'
              AND upg.status = 'ACTIVE'
              AND (upg.expires_at IS NULL OR upg.expires_at > UTC_TIMESTAMP())
              AND pg.enabled = 1
            """,
            (),
        )
        groups = await self._fetch(
            "SELECT id, code FROM permission_groups WHERE enabled = 1",
            (),
        )
        inherits = await self._fetch(
            "SELECT child_group_id, parent_group_id FROM permission_group_inherits",
            (),
        )
        legacy_admins = await self._fetch(
            """
            SELECT ga.user_id
            FROM global_admins ga
            INNER JOIN users u ON u.id = ga.user_id
            WHERE u.status <> 'BANNED'
            """,
            (),
        )

        group_codes = {int(row["id"]): row["code"] for row in groups}
        parents_by_child = {}
        for row in inherits:
            parents_by_child.setdefault(int(row["child_group_id"]), set()).add(
                int(row["parent_group_id"])
            )

        groups_by_user = {}
        for row in memberships:
            groups_by_user.setdefault(int(row["user_id"]), set()).add(
                int(row["group_id"])
            )

        superadmin_group_ids = {
            group_id
            for group_id, code in group_codes.items()
            if code == "platform.superadmin"
        }
        superadmin_user_ids = set()
        if superadmin_group_ids:
            superadmin_user_ids.update(int(row["user_id"]) for row in legacy_admins)
        for user_id, direct_group_ids in groups_by_user.items():
            effective_group_ids = set(direct_group_ids)
            pending = list(direct_group_ids)
            while pending:
                group_id = pending.pop()
                for parent_id in parents_by_child.get(group_id, set()):
                    if parent_id not in effective_group_ids:
                        effective_group_ids.add(parent_id)
                        pending.append(parent_id)
            if effective_group_ids & superadmin_group_ids:
                superadmin_user_ids.add(user_id)

        if not superadmin_user_ids:
            return []

        placeholders = ", ".join("%s" for _ in superadmin_user_ids)
        rows = await self._fetch(
            f"""
            SELECT DISTINCT qq.qq_number
            FROM user_qqs qq
            WHERE qq.user_id IN ({placeholders})
              AND qq.qq_number IS NOT NULL
              AND qq.qq_number <> ''
            ORDER BY qq.qq_number ASC
            """,
            tuple(sorted(superadmin_user_ids)),
        )
        return [str(row["qq_number"]) for row in rows]

    async def _fetch(self, sql, params):
        if self._pool is None:
            raise RuntimeError("MC ticket notifier database pool is not initialized")
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchall()

    def _format_daily_ticket(self, ticket):
        ticket_type = TICKET_TYPE_LABELS.get(ticket.get("type"), ticket.get("type") or "未知")
        server = self._format_server(ticket)
        creator = self._format_creator(ticket)
        view_group = self._format_view_group(ticket.get("view_group_code"))
        return (
            f"#{ticket['id']} | {ticket_type} | {self._clip(ticket.get('title'), 140)} | "
            f"发起人：{creator} | 服务器：{server} | "
            f"最低查看权限：{view_group} | "
            f"创建：{self._format_time(ticket.get('created_at'))} | "
            f"查看：{self._format_ticket_url(ticket['id'])}"
        )

    def _format_new_ticket(self, ticket, _scope=None):
        ticket_type = TICKET_TYPE_LABELS.get(ticket.get("type"), ticket.get("type") or "未知")
        view_label = self._policy_group_label(ticket.get("view_group_code"))
        participate_label = self._policy_group_label(ticket.get("participate_group_code"))
        manage_label = self._policy_group_label(ticket.get("manage_group_code"))
        permissions = f"查看-{view_label}｜参与-{participate_label}｜管理-{manage_label}"
        return (
            "【MC工单通知】新工单\n"
            f"工单 #{ticket['id']}：{self._clip(ticket.get('title'))}\n"
            f"类型：{ticket_type}\n"
            f"权限：{permissions}\n"
            f"发起人：{self._format_creator(ticket)}\n"
            f"服务器：{self._format_server(ticket)}\n"
            f"查看：{self._format_ticket_url(ticket['id'])}"
        )

    def _format_new_reply(self, reply, _scope=None):
        ticket_type = TICKET_TYPE_LABELS.get(reply.get("type"), reply.get("type") or "未知")
        return (
            "【MC工单通知】新回复\n"
            f"工单 #{reply['ticket_id']}：{self._clip(reply.get('title'))}\n"
            f"类型：{ticket_type}｜服务器：{self._format_server(reply)}\n"
            f"查看：{self._format_ticket_url(reply['ticket_id'])}"
        )

    @staticmethod
    def _notification_scopes(ticket):
        group_code = ticket.get("view_group_code")
        try:
            return VIEW_GROUP_AUDIENCES[group_code]
        except KeyError as exc:
            raise RuntimeError(
                f"unknown or missing MC ticket VIEW group: {group_code!r}"
            ) from exc

    @staticmethod
    def _policy_group_label(group_code):
        return {
            "PUBLIC": "公开",
            "mc.admin": "管理员",
            "mc.tech.admin": "技术管理员",
            "platform.superadmin": "服主",
        }.get(group_code, "未配置")

    @classmethod
    def _format_view_group(cls, group_code):
        return f"{cls._policy_group_label(group_code)}（{group_code}）"

    @staticmethod
    def _format_channel_scope(scope):
        try:
            label, group_code = NOTIFICATION_SCOPE_GROUPS[scope]
        except KeyError as exc:
            raise RuntimeError(
                f"unknown MC ticket notification scope: {scope!r}"
            ) from exc
        return f"当前通知渠道对应权限组：{label}（{group_code}）"

    @staticmethod
    def _format_server(ticket):
        if ticket.get("server_scope") == "ALL":
            return "综合"
        return str(ticket.get("source_server_name") or "指定服务器")

    @staticmethod
    def _format_creator(ticket):
        username = str(ticket.get("creator_username") or "").strip()
        nickname = str(ticket.get("creator_nickname") or "").strip()
        snapshot = str(ticket.get("creator_name") or "").strip()
        user_id = ticket.get("creator_user_id")
        name = username or nickname or snapshot or f"用户 {user_id}"
        if nickname and nickname != name:
            name = f"{name}（{nickname}）"
        return f"{name}（IGNG ID：{user_id}）"

    @staticmethod
    def _format_time(value):
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value or "-")

    def _format_ticket_url(self, ticket_id):
        base_url = self.config.MC_TICKET_ADMIN_URL
        separator = "&" if "?" in base_url else "?"
        if base_url.endswith(("?", "&")):
            separator = ""
        query = urlencode({"tab": "tickets-list", "ticketId": int(ticket_id)})
        return f"{base_url}{separator}{query}"

    @staticmethod
    def _split_messages(lines, max_length=3500):
        messages = []
        current = []
        current_length = 0
        for line in lines:
            line_length = len(line) + (1 if current else 0)
            if current and current_length + line_length > max_length:
                messages.append("\n".join(current))
                current = []
                current_length = 0
            current.append(line)
            current_length += len(line) + (1 if len(current) > 1 else 0)
        if current:
            messages.append("\n".join(current))
        return messages

    @classmethod
    def _split_daily_messages(cls, header_lines, ticket_lines, max_length=3500):
        header = "\n".join(header_lines)
        messages = []
        current = list(header_lines)
        current_length = len(header)
        for line in ticket_lines:
            line_length = len(line) + 1
            if len(current) > len(header_lines) and current_length + line_length > max_length:
                messages.append("\n".join(current))
                current = list(header_lines)
                current_length = len(header)
            current.append(line)
            current_length += line_length
        if len(current) > len(header_lines):
            messages.append("\n".join(current))
        return messages

    @staticmethod
    def _clip(value, limit=120):
        text = " ".join(str(value or "").split())
        # Ticket content is untrusted input. Prevent legacy CQ string segments
        # from becoming mentions or other OneBot actions in private messages.
        text = text.replace("[CQ:", "[CQ：")
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"
