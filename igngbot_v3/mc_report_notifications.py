import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomysql

from .onebot_api import send_group_text

logger = logging.getLogger(__name__)


class McReportNotifier:
    """Poll the shared MC reports database and notify the administrator group."""

    def __init__(self, config):
        self.config = config
        self._pool = None
        self._task = None
        self._daily_task = None
        self._last_report_id = 0
        self._last_reply_id = 0
        self._timezone = self._resolve_timezone(self.config.MC_REPORT_TIMEZONE)

    @staticmethod
    def _resolve_timezone(name):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            if name == "Asia/Shanghai":
                return timezone(timedelta(hours=8), "CST")
            raise

    async def start(self):
        self._pool = await aiomysql.create_pool(
            host=self.config.MC_REPORTS_DB_HOST,
            port=self.config.MC_REPORTS_DB_PORT,
            user=self.config.MC_REPORTS_DB_USER,
            password=self.config.MC_REPORTS_DB_PASSWORD,
            db=self.config.MC_REPORTS_DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=3,
        )
        self._last_report_id, self._last_reply_id = await self._get_current_cursors()
        self._task = asyncio.create_task(self._run(), name="mc-report-notifier")
        self._daily_task = asyncio.create_task(
            self._run_daily_reminder(),
            name="mc-report-daily-reminder",
        )
        logger.info(
            "MC report notifications enabled: group=%s, report_id=%s, reply_id=%s",
            self.config.MC_REPORT_NOTIFICATION_GROUP,
            self._last_report_id,
            self._last_reply_id,
        )
        logger.info(
            "MC report daily reminder scheduled at %02d:00 (%s)",
            self.config.MC_REPORT_DAILY_REMINDER_HOUR,
            self.config.MC_REPORT_TIMEZONE,
        )

    async def _get_current_cursors(self):
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute("SELECT COALESCE(MAX(id), 0) AS id FROM mc_reports")
                report_id = int((await cursor.fetchone())["id"])
                await cursor.execute("SELECT COALESCE(MAX(id), 0) AS id FROM mc_report_replies")
                reply_id = int((await cursor.fetchone())["id"])
        return report_id, reply_id

    async def _run(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MC report notification poll failed")
            await asyncio.sleep(self.config.MC_REPORT_POLL_INTERVAL)

    async def _run_daily_reminder(self):
        while True:
            now = datetime.now(self._timezone)
            target = now.replace(
                hour=self.config.MC_REPORT_DAILY_REMINDER_HOUR,
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
                logger.exception("MC report daily reminder failed")

    async def _send_daily_reminder(self):
        reports = await self._fetch(
            """
            SELECT id, title, created_at
            FROM mc_reports
            WHERE status = 'PENDING'
            ORDER BY created_at ASC, id ASC
            """,
            (),
        )
        if not reports:
            messages = ["【MC工单提醒】\n当前没有未结单工单。"]
        else:
            lines = [f"【MC工单提醒】当前未结单工单（{len(reports)} 条）"]
            lines.extend(
                f"#{report['id']} | {self._clip(report.get('title'), 180)} | "
                f"发起时间：{self._format_report_time(report.get('created_at'))}"
                for report in reports
            )
            messages = self._split_messages(lines)

        for message in messages:
            if not await send_group_text(
                self.config,
                self.config.MC_REPORT_NOTIFICATION_GROUP,
                message,
            ):
                raise RuntimeError("failed to send MC report daily reminder")

    @staticmethod
    def _format_report_time(value):
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value or "-")

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

    async def _poll_once(self):
        reports = await self._fetch(
            """
            SELECT id, title, content, source_server_name, reporter_user_id, reporter_name,
                   created_at
            FROM mc_reports
            WHERE id > %s
            ORDER BY id ASC
            LIMIT 20
            """,
            (self._last_report_id,),
        )
        for report in reports:
            message = self._format_new_report(report)
            if not await send_group_text(self.config, self.config.MC_REPORT_NOTIFICATION_GROUP, message):
                raise RuntimeError(f"failed to notify new MC report #{report['id']}")
            self._last_report_id = int(report["id"])

        replies = await self._fetch(
            """
            SELECT reply.id, reply.report_id, reply.author_name, reply.content, reply.created_at,
                   report.title
            FROM mc_report_replies AS reply
            INNER JOIN mc_reports AS report ON report.id = reply.report_id
            WHERE reply.id > %s
            ORDER BY reply.id ASC
            LIMIT 20
            """,
            (self._last_reply_id,),
        )
        for reply in replies:
            message = self._format_new_reply(reply)
            if not await send_group_text(self.config, self.config.MC_REPORT_NOTIFICATION_GROUP, message):
                raise RuntimeError(f"failed to notify MC report reply #{reply['id']}")
            self._last_reply_id = int(reply["id"])

    async def _fetch(self, sql, params):
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchall()

    def _format_new_report(self, report):
        server = report.get("source_server_name") or "未指定"
        return (
            "【MC工单通知】新工单\n"
            f"工单 #{report['id']}：{self._clip(report.get('title'))}\n"
            f"发起人：{self._clip(report.get('reporter_name'))} ({report.get('reporter_user_id')})\n"
            f"属地服务器：{self._clip(server)}\n"
            f"内容：{self._clip(report.get('content'), 500)}\n"
            f"查看：{self.config.MC_REPORTS_URL}/{report['id']}"
        )

    def _format_new_reply(self, reply):
        return (
            "【MC工单通知】新回复\n"
            f"工单 #{reply['report_id']}：{self._clip(reply.get('title'))}\n"
            f"回复人：{self._clip(reply.get('author_name'))}\n"
            f"内容：{self._clip(reply.get('content'), 500)}\n"
            f"查看：{self.config.MC_REPORTS_URL}/{reply['report_id']}"
        )

    @staticmethod
    def _clip(value, limit=120):
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"
