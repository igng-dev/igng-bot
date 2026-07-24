import logging
import re

import aiomysql

from .onebot_api import send_group_text

logger = logging.getLogger(__name__)


class McReportCommandHandler:
    """Handle group commands for creating and checking MC feedback tickets."""

    _COMMAND_RE = re.compile(r"^/服务器反馈(?:\s+(.*))?$")

    def __init__(self, config, notifier):
        self.config = config
        self.notifier = notifier
        self._identity_pool = None

    async def handle(self, parsed):
        content = str(parsed.get("message_content") or "").strip()
        match = self._COMMAND_RE.fullmatch(content)
        if not match:
            return False

        args = (match.group(1) or "").strip()
        if not args or args == "帮助":
            await self._send_help(parsed["group_id"])
            return True

        parts = args.split(maxsplit=2)
        action = parts[0]
        if action == "创建":
            if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
                await send_group_text(
                    self.config,
                    parsed["group_id"],
                    "用法：/服务器反馈 创建 目标玩家 原因",
                )
            else:
                await self._create(parsed, parts[1].strip(), parts[2].strip())
            return True

        if action == "待办":
            await self._send_pending(parsed)
            return True

        await self._send_help(parsed["group_id"])
        return True

    async def _send_help(self, group_id):
        await send_group_text(
            self.config,
            group_id,
            "服务器反馈使用帮助\n"
            "/服务器反馈 创建 目标玩家 原因\n"
            "创建一条反馈，所有管理员和涉事玩家可见。\n"
            "/服务器反馈 待办\n"
            "查看自己发起的未结单工单数量及时间信息。",
        )

    async def _create(self, parsed, target_name, reason):
        group_id = parsed["group_id"]
        try:
            identity = await self._find_identity(parsed["sender_id"])
            if identity is None:
                await send_group_text(
                    self.config,
                    group_id,
                    "创建失败：你的 QQ 尚未绑定 IGNG 账户，请先完成 QQ 绑定。",
                )
                return

            report_id = await self.notifier.create_public_report(
                user_id=identity["id"],
                reporter_name=identity["nickname"] or identity["username"] or parsed.get("sender_name") or str(parsed["sender_id"]),
                target_name=target_name,
                reason=reason,
            )
            await send_group_text(
                self.config,
                group_id,
                f"服务器反馈已创建，管理员和涉事玩家均可查看。\n工单编号：#{report_id}",
            )
        except Exception:
            logger.exception("Failed to create MC feedback from group command")
            await send_group_text(
                self.config,
                group_id,
                "服务器反馈创建失败，请稍后重试或联系管理员。",
            )

    async def _send_pending(self, parsed):
        try:
            identity = await self._find_identity(parsed["sender_id"])
            if identity is None:
                await send_group_text(
                    self.config,
                    parsed["group_id"],
                    "查询失败：你的 QQ 尚未绑定 IGNG 账户。",
                )
                return

            reports = await self.notifier.get_pending_reports_for_user(identity["id"])
            if not reports:
                message = "你发起的未结单工单数量：0"
            else:
                lines = [f"你发起的未结单工单数量：{len(reports)}"]
                for index, report in enumerate(reports, start=1):
                    latest = report["latest_reply_at"] or "暂无回复"
                    lines.append(
                        f"{index}. 创建时间：{report['created_at']}；最新回复时间：{latest}"
                    )
                message = "\n".join(lines)
            await send_group_text(self.config, parsed["group_id"], message)
        except Exception:
            logger.exception("Failed to query pending MC feedback from group command")
            await send_group_text(
                self.config,
                parsed["group_id"],
                "待办查询失败，请稍后重试或联系管理员。",
            )

    async def _find_identity(self, qq_number):
        if self._identity_pool is None:
            self._identity_pool = await aiomysql.create_pool(
                host=self.config.MC_REPORT_IDENTITY_DB_HOST,
                port=self.config.MC_REPORT_IDENTITY_DB_PORT,
                user=self.config.MC_REPORT_IDENTITY_DB_USER,
                password=self.config.MC_REPORT_IDENTITY_DB_PASSWORD,
                db=self.config.MC_REPORT_IDENTITY_DB_NAME,
                charset="utf8mb4",
                autocommit=True,
                minsize=1,
                maxsize=2,
            )
        async with self._identity_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT u.id, u.nickname, u.username
                    FROM user_qqs AS qq
                    INNER JOIN users AS u ON u.id = qq.user_id
                    WHERE qq.qq_number = %s
                    LIMIT 1
                    """,
                    (str(qq_number),),
                )
                return await cursor.fetchone()

    async def close(self):
        if self._identity_pool is not None:
            self._identity_pool.close()
            await self._identity_pool.wait_closed()
            self._identity_pool = None
