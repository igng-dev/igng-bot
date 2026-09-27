import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import igngbot_v3.mc_ticket_notifications as ticket_notifications
from igngbot_v3.onebot_api import _onebot_response_succeeded


class FakeNotificationStore:
    def __init__(self):
        self.cursors = {}
        self.deliveries = set()

    async def get_or_seed_cursor(self, stream, seed_id):
        self.cursors.setdefault(stream, int(seed_id))
        return self.cursors[stream]

    async def set_cursor(self, stream, last_id):
        self.cursors[stream] = max(self.cursors.get(stream, 0), int(last_id))

    async def was_delivered(self, event_key, destination_type, destination_id):
        return (event_key, destination_type, str(destination_id)) in self.deliveries

    async def mark_delivered(self, event_key, destination_type, destination_id):
        self.deliveries.add((event_key, destination_type, str(destination_id)))


class FakeOneBotResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body


class McTicketNotificationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            MC_TICKET_TIMEZONE="Asia/Shanghai",
            MC_TICKET_NOTIFICATION_GROUP=1000000007,
            MC_TICKET_TECH_NOTIFICATION_GROUP=1000000006,
            MC_TICKET_POLL_INTERVAL=10,
            MC_TICKET_DAILY_REMINDER_HOUR=10,
            MC_TICKET_ADMIN_URL="https://mc.igng.net/admin",
        )
        self.store = FakeNotificationStore()
        self.notifier = ticket_notifications.McTicketNotifier(
            self.config,
            state_store=self.store,
        )

    def test_view_policy_routes_are_explicit_and_fail_closed(self):
        self.assertEqual(
            self.notifier._notification_scopes({"view_group_code": "PUBLIC"}),
            ("ADMIN",),
        )
        self.assertEqual(
            self.notifier._notification_scopes({"view_group_code": "mc.admin"}),
            ("ADMIN",),
        )
        self.assertEqual(
            self.notifier._notification_scopes(
                {"view_group_code": "mc.tech.admin"}
            ),
            ("TECHADMIN",),
        )
        self.assertEqual(
            self.notifier._notification_scopes(
                {"view_group_code": "platform.superadmin"}
            ),
            ("SUPERADMIN",),
        )
        with self.assertRaisesRegex(RuntimeError, "unknown or missing"):
            self.notifier._notification_scopes({"view_group_code": None})
        with self.assertRaisesRegex(RuntimeError, "unknown or missing"):
            self.notifier._notification_scopes({"view_group_code": "mc.unknown"})

    def test_event_message_formats_three_permission_dimensions_and_cleans_visuals(self):
        sensitive = {
            "id": 12,
            "title": "服务器异常",
            "type": "REPORT",
            "server_scope": "SPECIFIC",
            "source_server_name": "生存服",
            "view_group_code": "platform.superadmin",
            "participate_group_code": "mc.tech.admin",
            "manage_group_code": "platform.superadmin",
            "creator_user_id": 31,
            "creator_name": "TianningZ",
            "creator_username": "Tianning",
            "creator_nickname": "天宁",
        }

        message = self.notifier._format_new_ticket(sensitive)

        for value in (
            "#12",
            "服务器异常",
            "类型：举报",
            "权限：查看-服主｜参与-技术管理员｜管理-服主",
            "Tianning",
            "天宁",
            "IGNG ID：31",
            "服务器：生存服",
            "ticketId=12",
        ):
            self.assertIn(value, message)

        self.assertNotIn("当前通知渠道对应权限组", message)
        self.assertNotIn("涉事玩家", message)
        self.assertNotIn("工单最低查看权限", message)
        self.assertNotIn("内容：", message)

    async def test_poll_routes_admin_tech_and_superadmin_by_view_policy(self):
        group_messages = []
        private_messages = []
        ticket_rows = [
            self._ticket_row(1, "mc.admin", "admin-secret"),
            self._ticket_row(2, "mc.tech.admin", "tech-secret"),
            self._ticket_row(3, "platform.superadmin", "owner-secret"),
        ]

        async def fake_fetch(sql, _params):
            if "FROM mc_tickets t" in sql:
                return ticket_rows
            if "FROM mc_ticket_messages m" in sql:
                return []
            raise AssertionError(f"unexpected query: {sql}")

        async def fake_group(_config, group_id, message):
            group_messages.append((int(group_id), message))
            return True

        async def fake_private(_config, user_id, message):
            private_messages.append((str(user_id), message))
            return True

        self.notifier._fetch = fake_fetch
        self.notifier._get_superadmin_qqs = AsyncMock(return_value=["1001"])
        with patch.object(ticket_notifications, "send_group_text", fake_group), patch.object(
            ticket_notifications, "send_private_text", fake_private
        ):
            await self.notifier._poll_once()

        self.assertEqual(
            [item[0] for item in group_messages],
            [1000000007, 1000000006],
        )
        self.assertEqual(
            [item[0] for item in private_messages],
            ["1001"],
        )
        self.assertIn("admin-secret", group_messages[0][1])
        self.assertIn("tech-secret", group_messages[1][1])
        self.assertIn("owner-secret", private_messages[0][1])
        self.assertNotIn("当前通知渠道对应权限组", group_messages[0][1])
        self.assertEqual(self.store.cursors["ticket"], 3)
        self.assertEqual(self.notifier._last_ticket_id, 3)

    async def test_partial_private_failure_retries_only_missing_destination(self):
        attempts = []
        second_target_attempts = 0

        async def fake_private(_config, user_id, _message):
            nonlocal second_target_attempts
            user_id = str(user_id)
            attempts.append(user_id)
            if user_id == "1002":
                second_target_attempts += 1
                return second_target_attempts > 1
            return True

        self.notifier._get_superadmin_qqs = AsyncMock(
            return_value=["1001", "1002"]
        )
        with patch.object(ticket_notifications, "send_private_text", fake_private):
            with self.assertRaisesRegex(RuntimeError, "1 target"):
                await self.notifier._send_visibility(
                    "SUPERADMIN",
                    "",
                    "private detail",
                    event_key="reply:8",
                )
            await self.notifier._send_visibility(
                "SUPERADMIN",
                "",
                "private detail",
                event_key="reply:8",
            )

        self.assertEqual(attempts.count("1001"), 1)
        self.assertEqual(attempts.count("1002"), 2)
        self.assertIn(("reply:8", "PRIVATE", "1001"), self.store.deliveries)
        self.assertIn(("reply:8", "PRIVATE", "1002"), self.store.deliveries)

    async def test_failed_group_delivery_does_not_advance_cursor(self):
        async def fake_fetch(sql, _params):
            if "FROM mc_tickets t" in sql:
                return [self._ticket_row(7, "mc.admin", "sensitive")]
            if "FROM mc_ticket_messages m" in sql:
                return []
            raise AssertionError(f"unexpected query: {sql}")

        async def failed_group(_config, _group_id, _message):
            return False

        self.notifier._fetch = fake_fetch
        with patch.object(ticket_notifications, "send_group_text", failed_group):
            with self.assertRaisesRegex(RuntimeError, "failed to send"):
                await self.notifier._poll_once()

        self.assertEqual(self.notifier._last_ticket_id, 0)
        self.assertNotIn("ticket", self.store.cursors)
        self.assertNotIn(
            ("ticket:7", "GROUP", "1000000007"),
            self.store.deliveries,
        )

    async def test_reply_routes_to_tech_group_from_view_policy(self):
        group_messages = []
        private_messages = []

        async def fake_fetch(sql, _params):
            if "FROM mc_tickets t" in sql:
                return []
            if "FROM mc_ticket_messages m" in sql:
                return [
                    {
                        "id": 9,
                        "ticket_id": 7,
                        "author_name": "reply-author",
                        "content": "reply-secret",
                        "created_at": None,
                        "title": "reply-title",
                        "type": "APPEAL",
                        "server_scope": "ALL",
                        "source_server_name": None,
                        "status": "OPEN",
                        "view_group_code": "mc.tech.admin",
                    }
                ]
            raise AssertionError(f"unexpected query: {sql}")

        async def fake_group(_config, group_id, message):
            group_messages.append((int(group_id), message))
            return True

        async def fake_private(_config, user_id, message):
            private_messages.append((str(user_id), message))
            return True

        self.notifier._fetch = fake_fetch
        self.notifier._get_superadmin_qqs = AsyncMock(return_value=["1001"])
        with patch.object(ticket_notifications, "send_group_text", fake_group), patch.object(
            ticket_notifications, "send_private_text", fake_private
        ):
            await self.notifier._poll_once()

        self.assertEqual([item[0] for item in group_messages], [1000000006])
        self.assertEqual(len(private_messages), 0)
        self.assertIn("reply-title", group_messages[0][1])
        self.assertIn("类型：申诉｜服务器：综合", group_messages[0][1])
        self.assertIn("ticketId=7", group_messages[0][1])
        self.assertNotIn("reply-secret", group_messages[0][1])
        self.assertNotIn("当前通知渠道对应权限组", group_messages[0][1])
        self.assertEqual(self.notifier._last_message_id, 9)
        self.assertEqual(self.store.cursors["reply"], 9)

    async def test_cursor_seed_is_not_overwritten_after_restart(self):
        first = await self.store.get_or_seed_cursor("ticket", 20)
        await self.store.set_cursor("ticket", 24)
        after_restart = await self.store.get_or_seed_cursor("ticket", 99)

        self.assertEqual(first, 20)
        self.assertEqual(after_restart, 24)

    async def test_daily_reminder_reads_view_policy_only(self):
        queries = []
        sends = []

        async def fake_fetch(sql, _params):
            queries.append(sql)
            return [
                self._ticket_row(1, "mc.admin", "ticket-alpha"),
                self._ticket_row(2, "mc.tech.admin", "ticket-beta"),
                self._ticket_row(3, "platform.superadmin", "ticket-gamma"),
            ]

        async def fake_send(scope, group_message, private_message, event_key):
            sends.append((scope, group_message, private_message, event_key))

        self.notifier._fetch = fake_fetch
        self.notifier._send_visibility = fake_send
        await self.notifier._send_daily_reminder()

        self.assertIn("p.action = 'VIEW'", queries[0])
        self.assertNotIn("MANAGE", queries[0])
        self.assertEqual([item[0] for item in sends], ["ADMIN", "TECHADMIN", "SUPERADMIN"])
        self.assertTrue(all("daily:" in item[3] for item in sends))
        admin_message = sends[0][1]
        tech_message = sends[1][1]
        owner_message = sends[2][2]
        self.assertIn("ticket-alpha", admin_message)
        self.assertNotIn("ticket-beta", admin_message)
        self.assertNotIn("ticket-gamma", admin_message)
        self.assertNotIn("ticket-alpha", tech_message)
        self.assertIn("ticket-beta", tech_message)
        self.assertNotIn("ticket-gamma", tech_message)
        self.assertNotIn("ticket-alpha", owner_message)
        self.assertNotIn("ticket-beta", owner_message)
        self.assertIn("ticket-gamma", owner_message)
        self.assertIn(
            "当前通知渠道对应权限组：技术管理员（mc.tech.admin）",
            tech_message,
        )
        self.assertIn("最低查看权限：技术管理员（mc.tech.admin）", tech_message)

    async def test_daily_reminder_sends_nothing_without_open_tickets(self):
        self.notifier._fetch = AsyncMock(return_value=[])
        self.notifier._send_visibility = AsyncMock()

        await self.notifier._send_daily_reminder()

        self.notifier._send_visibility.assert_not_awaited()

    async def test_onebot_success_requires_ok_status_and_zero_retcode(self):
        self.assertTrue(
            await _onebot_response_succeeded(
                FakeOneBotResponse(200, '{"status":"ok","retcode":0,"data":{}}'),
                "test",
            )
        )
        self.assertFalse(
            await _onebot_response_succeeded(
                FakeOneBotResponse(200, '{"status":"failed","retcode":100}'),
                "test",
            )
        )
        self.assertFalse(
            await _onebot_response_succeeded(
                FakeOneBotResponse(500, '{"status":"ok","retcode":0}'),
                "test",
            )
        )
        self.assertFalse(
            await _onebot_response_succeeded(
                FakeOneBotResponse(200, "not-json"),
                "test",
            )
        )

    @staticmethod
    def _ticket_row(ticket_id, view_group_code, title):
        return {
            "id": ticket_id,
            "title": title,
            "type": "FEEDBACK",
            "server_scope": "ALL",
            "source_server_name": None,
            "status": "OPEN",
            "view_group_code": view_group_code,
            "participate_group_code": view_group_code,
            "manage_group_code": view_group_code,
            "creator_user_id": 10,
            "creator_name": "user10",
            "creator_username": "user10",
            "creator_nickname": "",
        }


if __name__ == "__main__":
    unittest.main()
