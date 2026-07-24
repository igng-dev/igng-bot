import logging
import re
import threading
from datetime import datetime, timedelta

import pymysql
import requests
from pymysql.cursors import DictCursor

from .api_clients import cloud_chat_completion

logger = logging.getLogger(__name__)

DB1_HOST = "rm-2ze7v7v0evurv0953po.mysql.rds.aliyuncs.com"
DB2_HOST = "rm-rj94w0fari8g50ztf4o.mysql.rds-aliyun-america.rds.aliyuncs.com"
DB_USER = "igng_bot"

DEFAULT_NOTIFY_GROUPS = {1000000004, 1000000005}
GROUP_ID_RE = re.compile(r"^\d+(?:,\d+)*$")
TIME_PERIOD_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")

SERVER_LIST_CMD = re.compile(r"/服务器列表")
SERVER_STATUS_CMD = re.compile(r"/服务器状态(\s+(.+))?")
FAKE_LIST_CMD = re.compile(r"/假人列表(\s+(.+))?")
MY_FAKE_CMD = re.compile(r"/我的假人(\s+(.+))?")
BBS_QUEUE_CMD = re.compile(r"/BBS队列")
LAND_LIST_CMD = re.compile(r"/领地列表\s+(.+)")
NOTIFY_CMD = re.compile(r"/通知\s+(.+)")
SUMMARY_CMD = re.compile(r"/总结\s+(\S+)(?:\s+(.+))?")
IGNG_ACCOUNT_CMD = re.compile(r"/IGNG账户")


class IGNGQueryHandler:
    def __init__(self, config, db=None):
        self.config = config
        self.db = db
        self._conn1 = None  # DB1: mc_status, mc_account
        self._conn2 = None  # DB2: igng_sites
        self._lock = threading.Lock()

    def _ensure_conn1(self):
        if self._conn1 is None:
            self._conn1 = pymysql.connect(
                host=DB1_HOST,
                user=DB_USER,
                password=self.config.DB_PASSWORD,
                charset="utf8mb4",
                cursorclass=DictCursor,
            )
        self._conn1.ping(reconnect=True)
        return self._conn1

    def _ensure_conn2(self):
        if self._conn2 is None:
            self._conn2 = pymysql.connect(
                host=DB2_HOST,
                user=DB_USER,
                password=self.config.DB_PASSWORD,
                charset="utf8mb4",
                cursorclass=DictCursor,
            )
        self._conn2.ping(reconnect=True)
        return self._conn2

    def _send_reply(self, group_id, text):
        try:
            group_id = int(group_id)
            endpoint = "send_private_msg" if group_id < 0 else "send_group_msg"
            target = {"user_id": -group_id} if group_id < 0 else {"group_id": group_id}
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}",
                "Content-Type": "application/json",
            }
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/{endpoint}",
                json={**target, "message": text},
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send reply: {e}")

    # --- Command dispatch ---

    def handle_command(self, parsed):
        content = parsed.get("message_content", "").strip()
        sender_id = parsed.get("sender_id", 0)
        group_id = parsed.get("group_id", 0)
        if not group_id:
            return False

        is_admin = self.db.is_bot_admin(sender_id) if self.db else False

        # /IGNG账户
        if IGNG_ACCOUNT_CMD.fullmatch(content):
            logger.info(f"IGNG cmd: IGNG账户 from {sender_id}")
            threading.Thread(
                target=self._cmd_igng_account,
                args=(group_id, sender_id),
                daemon=True,
            ).start()
            return True

        # /服务器列表
        if SERVER_LIST_CMD.search(content):
            logger.info(f"IGNG cmd: 服务器列表 from {sender_id} in group {group_id}")
            threading.Thread(target=self._cmd_server_list, args=(group_id,), daemon=True).start()
            return True

        # /服务器状态 [服务器名]
        m = SERVER_STATUS_CMD.search(content)
        if m:
            server_name = m.group(2)
            logger.info(f"IGNG cmd: 服务器状态 server={server_name} from {sender_id}")
            threading.Thread(target=self._cmd_server_status, args=(group_id, server_name), daemon=True).start()
            return True

        # /假人列表 [服务器名]
        m = FAKE_LIST_CMD.search(content)
        if m:
            server_name = m.group(2)
            logger.info(f"IGNG cmd: 假人列表 server={server_name} from {sender_id}")
            threading.Thread(target=self._cmd_fake_list, args=(group_id, server_name), daemon=True).start()
            return True

        # /我的假人 [服务器名]
        m = MY_FAKE_CMD.search(content)
        if m:
            server_name = m.group(2)
            logger.info(f"IGNG cmd: 我的假人 server={server_name} from {sender_id}")
            threading.Thread(target=self._cmd_my_fake, args=(group_id, sender_id, server_name), daemon=True).start()
            return True

        # /BBS队列
        if BBS_QUEUE_CMD.search(content):
            logger.info(f"IGNG cmd: BBS队列 from {sender_id}")
            threading.Thread(target=self._cmd_bbs_queue, args=(group_id,), daemon=True).start()
            return True

        # /领地列表 <玩家名>
        m = LAND_LIST_CMD.search(content)
        if m:
            player_name = m.group(1).strip()
            logger.info(f"IGNG cmd: 领地列表 player={player_name} from {sender_id}")
            threading.Thread(target=self._cmd_land_list, args=(group_id, player_name), daemon=True).start()
            return True

        # /通知 <消息> [群号列表] (admin only)
        m = NOTIFY_CMD.search(content)
        if m:
            logger.info(f"IGNG cmd: 通知 from {sender_id}")
            if not is_admin:
                self._send_reply(group_id, "只有bot管理员可以使用此命令")
                return True
            threading.Thread(target=self._cmd_notify, args=(group_id, m.group(1)), daemon=True).start()
            return True

        # /总结 <时间> [群号列表] (admin only)
        m = SUMMARY_CMD.search(content)
        if m:
            logger.info(f"IGNG cmd: 总结 from {sender_id}")
            if not is_admin:
                self._send_reply(group_id, "只有bot管理员可以使用此命令")
                return True
            threading.Thread(
                target=self._cmd_summary, args=(group_id, m.group(1), m.group(2)),
                daemon=True,
            ).start()
            return True

        return False

    # --- Command implementations ---

    def _cmd_igng_account(self, group_id, sender_id):
        try:
            with self._lock:
                conn = self._ensure_conn2()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.id, u.nickname, u.username, COALESCE(e.exp, 0) AS exp
                    FROM igng_sites.user_qqs AS qq
                    INNER JOIN igng_sites.users AS u ON u.id = qq.user_id
                    LEFT JOIN igng_sites.igng_user_exp AS e ON e.user_id = u.id
                    WHERE qq.qq_number = %s
                    LIMIT 1
                    """,
                    (str(sender_id),),
                )
                row = cur.fetchone()
            if not row:
                self._send_reply(
                    group_id,
                    "当前 QQ 尚未绑定 IGNG 账户，请先在 IGNG 网站绑定 QQ。",
                )
                return

            exp = int(row.get("exp") or 0)
            level = exp // 100 + 1
            nickname = row.get("nickname") or row.get("username") or "未设置"
            self._send_reply(
                group_id,
                f"IGNG账户信息：\n用户ID：{row['id']}\n昵称：{nickname}\n等级：{level}",
            )
        except Exception as e:
            logger.error(f"IGNG账户查询失败: {e}", exc_info=True)
            self._send_reply(group_id, "IGNG账户查询失败，请稍后再试。")

    def _cmd_server_list(self, group_id):
        self._send_reply(group_id, "正在查询服务器列表...")
        try:
            with self._lock:
                conn = self._ensure_conn1()
            with conn.cursor() as cur:
                cur.execute("SELECT server_name FROM mc_status.servers ORDER BY server_id")
                rows = cur.fetchall()
            if not rows:
                self._send_reply(group_id, "暂无服务器")
                return
            lines = ["当前服务器列表："]
            for i, r in enumerate(rows, 1):
                lines.append(f"  {i}. {r['server_name']}")
            self._send_reply(group_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"服务器列表 error: {e}", exc_info=True)
            self._send_reply(group_id, "查询服务器列表失败，请稍后再试")

    def _cmd_server_status(self, group_id, server_name):
        self._send_reply(group_id, "正在查询服务器状态...")
        try:
            with self._lock:
                conn = self._ensure_conn1()
            with conn.cursor() as cur:
                if server_name:
                    self._send_server_detail(conn, cur, group_id, server_name.strip())
                else:
                    self._send_server_summary(conn, cur, group_id)
        except Exception as e:
            logger.error(f"服务器状态 error: {e}", exc_info=True)
            self._send_reply(group_id, "查询服务器状态失败，请稍后再试")

    def _send_server_summary(self, conn, cur, group_id):
        cur.execute("SELECT server_id, server_name FROM mc_status.servers ORDER BY server_id")
        servers = cur.fetchall()
        if not servers:
            self._send_reply(group_id, "暂无服务器")
            return

        lines = ["服务器状态概览："]
        for srv in servers:
            sid = srv["server_id"]
            sname = srv["server_name"]

            # Latest Beijing (node 1) latency
            cur.execute(
                "SELECT avg_latency_ms, packet_loss_pct FROM mc_status.latencies "
                "WHERE node_id = 1 AND server_id = %s "
                "ORDER BY timestamp_utc DESC LIMIT 1",
                (sid,),
            )
            lat = cur.fetchone()

            # Latest performance
            cur.execute(
                "SELECT avg_tps, avg_mspt, cpu_usage, memory_usage_mb, online_players "
                "FROM mc_status.server_performance_logs WHERE server_id = %s "
                "ORDER BY recorded_at DESC LIMIT 1",
                (sid,),
            )
            perf = cur.fetchone()

            if lat and lat["packet_loss_pct"] >= 100:
                lat_str = "不可达"
            elif lat:
                lat_str = f"{lat['avg_latency_ms']}ms"
            else:
                lat_str = "无数据"

            if perf:
                perf_str = (
                    f"TPS:{perf['avg_tps']:.0f} MSPT:{perf['avg_mspt']:.1f} "
                    f"CPU:{perf['cpu_usage']:.1f}% 内存:{perf['memory_usage_mb']:.0f}MB "
                    f"在线:{perf['online_players']}"
                )
            else:
                perf_str = "无数据"

            lines.append(f"\n【{sname}】")
            lines.append(f"  北京延迟: {lat_str}")
            lines.append(f"  性能: {perf_str}")

        self._send_reply(group_id, "\n".join(lines))

    def _send_server_detail(self, conn, cur, group_id, server_name):
        cur.execute(
            "SELECT server_id, server_name FROM mc_status.servers WHERE server_name = %s",
            (server_name,),
        )
        srv = cur.fetchone()
        if not srv:
            self._send_reply(group_id, f"未找到服务器「{server_name}」")
            return

        sid = srv["server_id"]

        # All nodes latest latency
        cur.execute(
            "SELECT ln.node_name, l.avg_latency_ms, l.max_latency_ms, l.min_latency_ms, l.packet_loss_pct "
            "FROM mc_status.latencies l "
            "JOIN mc_status.latency_nodes ln ON l.node_id = ln.node_id "
            "WHERE l.server_id = %s "
            "AND l.timestamp_utc = ("
            "  SELECT MAX(timestamp_utc) FROM mc_status.latencies l2 "
            "  WHERE l2.server_id = l.server_id AND l2.node_id = l.node_id"
            ") "
            "ORDER BY l.node_id",
            (sid,),
        )
        nodes = cur.fetchall()

        # Recent 15 min performance
        cur.execute(
            "SELECT avg_tps, avg_mspt, cpu_usage, memory_usage_mb, online_players, recorded_at "
            "FROM mc_status.server_performance_logs WHERE server_id = %s "
            "AND recorded_at >= NOW() - INTERVAL 15 MINUTE "
            "ORDER BY recorded_at DESC",
            (sid,),
        )
        perfs = cur.fetchall()

        lines = [f"「{server_name}」详细状态："]
        if nodes:
            lines.append("\n节点延迟：")
            for n in nodes:
                if n["packet_loss_pct"] >= 100:
                    lat_str = f"不可达(丢包{n['packet_loss_pct']:.0f}%)"
                else:
                    lat_str = (
                        f"平均{n['avg_latency_ms']}ms 最慢{n['max_latency_ms']}ms "
                        f"最快{n['min_latency_ms']}ms 丢包{n['packet_loss_pct']:.0f}%"
                    )
                lines.append(f"  {n['node_name']}: {lat_str}")
        else:
            lines.append("\n节点延迟：无数据")

        if perfs:
            lines.append(f"\n最近15分钟性能（共{len(perfs)}条）：")
            # Show first 5 entries
            for p in perfs[:5]:
                t = p["recorded_at"].strftime("%H:%M:%S")
                lines.append(
                    f"  [{t}] TPS:{p['avg_tps']:.0f} MSPT:{p['avg_mspt']:.1f} "
                    f"CPU:{p['cpu_usage']:.1f}% 内存:{p['memory_usage_mb']:.0f}MB "
                    f"在线:{p['online_players']}"
                )
        else:
            lines.append("\n最近15分钟性能：无数据")

        self._send_reply(group_id, "\n".join(lines))

    def _cmd_fake_list(self, group_id, server_name):
        self._send_reply(group_id, "正在查询假人列表...")
        try:
            with self._lock:
                conn = self._ensure_conn1()
            with conn.cursor() as cur:
                if server_name:
                    server_name = server_name.strip()
                    cur.execute(
                        "SELECT f.fake_name, f.creator_name, f.world "
                        "FROM mc_status.fake_player_list f "
                        "JOIN mc_status.servers s ON f.server_id = s.server_id "
                        "WHERE s.server_name = %s "
                        "ORDER BY f.fake_name",
                        (server_name,),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        self._send_reply(group_id, f"服务器「{server_name}」暂无假人")
                        return
                    lines = [f"「{server_name}」假人列表："]
                else:
                    cur.execute(
                        "SELECT f.fake_name, f.creator_name, f.world, s.server_name "
                        "FROM mc_status.fake_player_list f "
                        "JOIN mc_status.servers s ON f.server_id = s.server_id "
                        "ORDER BY s.server_name, f.fake_name",
                    )
                    rows = cur.fetchall()
                    if not rows:
                        self._send_reply(group_id, "暂无假人")
                        return
                    lines = ["全部假人列表："]

                for r in rows:
                    owner = r["creator_name"]
                    world = r["world"]
                    name = r["fake_name"]
                    if server_name:
                        lines.append(f"  {name} | 世界:{world} | 主人:{owner}")
                    else:
                        lines.append(f"  [{r['server_name']}] {name} | 世界:{world} | 主人:{owner}")

            self._send_reply(group_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"假人列表 error: {e}", exc_info=True)
            self._send_reply(group_id, "查询假人列表失败，请稍后再试")

    def _cmd_my_fake(self, group_id, sender_id, server_name):
        self._send_reply(group_id, "正在查询你的假人...")
        try:
            with self._lock:
                conn1 = self._ensure_conn1()
                conn2 = self._ensure_conn2()

            # Step 1: QQ -> IGNg user_id
            with conn2.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM igng_sites.user_qqs WHERE qq_number = %s",
                    (str(sender_id),),
                )
                uq = cur.fetchone()

            if not uq:
                self._send_reply(group_id, "你尚未绑定IGNG账号，请先在IGNG网站绑定QQ")
                return

            igng_id = uq["user_id"]

            # Step 2: IGNg user_id -> MC account names
            with conn1.cursor() as cur:
                cur.execute(
                    "SELECT mc_username FROM mc_account.mc_mapping WHERE igng_id = %s",
                    (igng_id,),
                )
                accounts = cur.fetchall()

            if not accounts:
                self._send_reply(group_id, "你尚未绑定Minecraft账号")
                return

            mc_names = [a["mc_username"] for a in accounts]

            # Step 3: MC account names -> fake players
            with conn1.cursor() as cur:
                if server_name:
                    server_name = server_name.strip()
                    cur.execute(
                        "SELECT f.fake_name, f.world, f.health, f.hunger "
                        "FROM mc_status.fake_player_list f "
                        "JOIN mc_status.servers s ON f.server_id = s.server_id "
                        "WHERE f.creator_name IN %(names)s AND s.server_name = %(srv)s "
                        "ORDER BY f.fake_name",
                        {"names": tuple(mc_names), "srv": server_name},
                    )
                else:
                    cur.execute(
                        "SELECT f.fake_name, f.world, f.health, f.hunger, s.server_name "
                        "FROM mc_status.fake_player_list f "
                        "JOIN mc_status.servers s ON f.server_id = s.server_id "
                        "WHERE f.creator_name IN %(names)s "
                        "ORDER BY s.server_name, f.fake_name",
                        {"names": tuple(mc_names)},
                    )
                rows = cur.fetchall()

            if not rows:
                self._send_reply(group_id, "你暂无假人")
                return

            if server_name:
                lines = [f"你在「{server_name}」的假人："]
            else:
                lines = ["你的假人："]
            for r in rows:
                health_str = f"{r['health']:.0f}" if r["health"] == int(r["health"]) else f"{r['health']:.1f}"
                if server_name:
                    lines.append(
                        f"  {r['fake_name']} | 世界:{r['world']} "
                        f"| 生命:{health_str} | 饱和度:{r['hunger']}"
                    )
                else:
                    lines.append(
                        f"  [{r['server_name']}] {r['fake_name']} | 世界:{r['world']} "
                        f"| 生命:{health_str} | 饱和度:{r['hunger']}"
                    )

            self._send_reply(group_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"我的假人 error: {e}", exc_info=True)
            self._send_reply(group_id, "查询我的假人失败，请稍后再试")

    def _cmd_bbs_queue(self, group_id):
        self._send_reply(group_id, "正在查询BBS审核队列...")
        try:
            with self._lock:
                conn2 = self._ensure_conn2()
            with conn2.cursor() as cur:
                cur.execute(
                    "SELECT rq.id, rq.target_type, rq.target_id, rq.review_type, "
                    "rq.status, rq.reporter_id, rq.created_at, "
                    "COALESCE(post_user.nickname, bio_user.nickname, "
                    "  post_user.username, bio_user.username) AS author_name "
                    "FROM igng_sites.review_queue rq "
                    "LEFT JOIN igng_sites.posts p ON rq.target_id = p.post_id AND rq.target_type = 'post' "
                    "LEFT JOIN igng_sites.users post_user ON p.user_id = post_user.id "
                    "LEFT JOIN igng_sites.users bio_user ON rq.target_id = bio_user.id AND rq.target_type = 'user_bio' "
                    "WHERE rq.status = 'pending' "
                    "ORDER BY rq.created_at ASC"
                )
                rows = cur.fetchall()

            if not rows:
                self._send_reply(group_id, "审核队列为空")
                return

            lines = [f"当前审核队列（共{len(rows)}项）："]
            for r in rows:
                target = f"{r['target_type']}#{r['target_id']}"
                review = r["review_type"]
                author = r["author_name"] or "未知"
                created = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"

                lines.append(
                    f"  #{r['id']} [{review}] {target} | 作者:{author} | 提交:{created}"
                )

            self._send_reply(group_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"BBS队列 error: {e}", exc_info=True)
            self._send_reply(group_id, "查询审核队列失败，请稍后再试")

    def _cmd_land_list(self, group_id, player_name):
        self._send_reply(group_id, f"正在查询玩家「{player_name}」的领地...")
        try:
            with self._lock:
                conn = self._ensure_conn1()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT l.land_name, l.world, l.description, s.server_name "
                    "FROM mc_account.land_records l "
                    "JOIN mc_status.servers s ON l.server_id = s.server_id "
                    "WHERE l.owner_name = %s AND l.is_public = 1 "
                    "ORDER BY s.server_name, l.land_name",
                    (player_name,),
                )
                rows = cur.fetchall()

            if not rows:
                self._send_reply(group_id, f"玩家「{player_name}」暂无公开领地")
                return

            lines = [f"「{player_name}」的公开领地（共{len(rows)}个）："]
            for r in rows:
                desc = f" | {r['description']}" if r["description"] else ""
                lines.append(
                    f"  {r['land_name']} | {r['server_name']} | {r['world']}{desc}"
                )

            self._send_reply(group_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"领地列表 error: {e}", exc_info=True)
            self._send_reply(group_id, "查询领地列表失败，请稍后再试")

    # --- Admin commands ---

    def _can_mention_all(self, group_id):
        try:
            headers = {"Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}"}
            response = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/get_group_member_info",
                json={
                    "group_id": int(group_id),
                    "user_id": int(self.config.BOT_USER_ID),
                    "no_cache": False,
                },
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            role = ((result.get("data") or {}).get("role") or "").lower()
            return result.get("retcode") in (None, 0) and role in {"admin", "owner"}
        except Exception as exc:
            logger.warning("Failed to check @all permission in group %s: %s", group_id, exc)
            return False

    def _send_group_msg(self, group_id, text, mention_all=False):
        """Send message to any group (for admin commands like notify/summary)."""
        try:
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}",
                "Content-Type": "application/json",
            }
            message = text
            if mention_all:
                message = [
                    {"type": "at", "data": {"qq": "all"}},
                    {"type": "text", "data": {"text": text}},
                ]
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/send_group_msg",
                json={"group_id": int(group_id), "message": message},
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            try:
                result = r.json()
            except ValueError:
                result = None
            if isinstance(result, dict):
                retcode = result.get("retcode")
                status = result.get("status")
                if retcode not in (None, 0) or status not in (None, "ok"):
                    logger.error(
                        "OneBot rejected group %s notification: %s",
                        group_id,
                        result,
                    )
                    return False
            else:
                logger.warning(
                    "OneBot returned a non-JSON response for group %s: HTTP %s %s",
                    group_id,
                    r.status_code,
                    r.text[:500],
                )
            return True
        except Exception as e:
            logger.error(f"Failed to send to group {group_id}: {e}")
            return False

    def _parse_group_ids_from_tail(self, raw):
        """Parse trailing group IDs from command argument.
        If the last token matches '123,456' format, return (message_part, [ids]).
        Otherwise return (raw, None)."""
        if not raw:
            return raw, None
        raw = raw.strip()
        parts = raw.rsplit(" ", 1)
        if len(parts) == 2 and GROUP_ID_RE.match(parts[1]):
            ids = [int(x.strip()) for x in parts[1].split(",") if x.strip()]
            return parts[0].strip(), ids
        if GROUP_ID_RE.match(raw):
            return "", [int(x.strip()) for x in raw.split(",") if x.strip()]
        return raw, None

    def _parse_time_period(self, s):
        """Parse time period string like '1d', '12h', '30m', '1d12h' into timedelta.
        Returns timedelta or None if invalid."""
        m = TIME_PERIOD_RE.fullmatch(s.strip())
        if not m:
            return None
        d = int(m.group(1)) if m.group(1) else 0
        h = int(m.group(2)) if m.group(2) else 0
        mi = int(m.group(3)) if m.group(3) else 0
        if d == 0 and h == 0 and mi == 0:
            return None
        return timedelta(days=d, hours=h, minutes=mi)

    def _cmd_notify(self, source_group_id, raw_args):
        """Admin command: /通知 <消息> [群号列表]"""
        try:
            mention_all = bool(re.search(r"(?:^|\s)全员(?:\s|$)", raw_args))
            raw_args = re.sub(r"(?:^|\s)全员(?=\s|$)", " ", raw_args).strip()
            msg, target_ids = self._parse_group_ids_from_tail(raw_args)
            if not msg:
                self._send_reply(source_group_id, "用法：/通知 通知内容 [全员] [群号,群号...]")
                return

            if target_ids is None:
                target_ids = sorted(DEFAULT_NOTIFY_GROUPS)

            success_groups = []
            failed_groups = []
            mention_failed_groups = []
            for gid in target_ids:
                can_mention_all = not mention_all or self._can_mention_all(gid)
                if mention_all and not can_mention_all:
                    mention_failed_groups.append(str(gid))
                if self._send_group_msg(gid, msg, mention_all=mention_all and can_mention_all):
                    success_groups.append(str(gid))
                else:
                    failed_groups.append(str(gid))

            feedback = f"通知已发送至: {', '.join(success_groups)}"
            if failed_groups:
                feedback += f"\n发送失败: {', '.join(failed_groups)}"
            if mention_failed_groups:
                feedback += (
                    "\n以下群机器人不是管理员，无法发送@全体成员，已仅发送正文: "
                    + ", ".join(mention_failed_groups)
                )
            self._send_reply(source_group_id, feedback)
        except Exception as e:
            logger.error(f"通知 error: {e}", exc_info=True)
            self._send_reply(source_group_id, "发送通知失败")

    def _cmd_summary(self, group_id, time_str, raw_groups):
        """Admin command: /总结 <时间> [群号列表]"""
        try:
            # Parse group IDs
            target_ids = None
            if raw_groups:
                _, target_ids = self._parse_group_ids_from_tail(raw_groups)
            if target_ids is None:
                target_ids = [group_id]

            # Parse time period
            delta = self._parse_time_period(time_str)
            if delta is None:
                self._send_reply(group_id, "时间格式错误，示例：1d / 12h / 30m / 1d12h")
                return

            since = datetime.now() - delta
            self._send_reply(group_id, f"正在总结最近 {time_str} 的聊天内容，请稍候...")

            # Query messages (limit to prevent context overflow)
            with self._lock:
                conn = self._ensure_conn1()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sender_id, message_content, created_at "
                    "FROM igng_bot.message_logs "
                    "WHERE group_id = %s AND created_at >= %s "
                    "AND message_content IS NOT NULL AND message_content != '' "
                    "ORDER BY created_at ASC "
                    "LIMIT 200",
                    (group_id, since),
                )
                rows = cur.fetchall()

            if not rows:
                for gid in target_ids:
                    self._send_group_msg(gid, f"本群最近 {time_str} 暂无聊天记录")
                return

            # Build chat transcript, keeping total under ~10K chars for model context
            MAX_LINE_CHARS = 80
            MAX_TOTAL_CHARS = 10000
            lines = []
            total = 0
            for r in rows:
                t = r["created_at"].strftime("%m-%d %H:%M") if r["created_at"] else "?"
                content = r["message_content"]
                if len(content) > MAX_LINE_CHARS:
                    content = content[:MAX_LINE_CHARS]
                sender = str(r["sender_id"])
                line = f"[{t}] {sender}: {content}"
                if total + len(line) > MAX_TOTAL_CHARS:
                    break
                lines.append(line)
                total += len(line)

            chat_text = "\n".join(lines)
            logger.info(f"Summary chat_text: {len(lines)} msgs, {total} chars")

            # Call Ollama for summary
            summary = self._call_ollama_summary(chat_text, time_str)

            for gid in target_ids:
                self._send_group_msg(gid, summary)
        except Exception as e:
            logger.error(f"总结 error: {e}", exc_info=True)
            self._send_reply(group_id, "总结失败，请稍后再试")

    def _call_ollama_summary(self, chat_text, time_str):
        """Send chat transcript to cloud LLM and return the summary."""
        system_msg = (
            "你是一个群聊总结助手。用户会给你一段群聊记录，请你总结其中的主要内容。"
            "规则：1) 按主题分点总结，每个主题一句话概括；"
            "2) 忽略琐碎无意义的闲聊（如单独的表情、\"好\"、\"嗯\"等）；"
            "3) 突出有价值的问题、讨论、分享、公告；"
            "4) 如果整个聊天记录都是无意义的闲聊，就回复\"这段时间主要是闲聊，没有特别值得总结的内容\"；"
            "5) 用中文输出，简洁明了，不超过500字；"
            "6) 只输出总结内容本身，禁止反问、追问、建议、表情符号、\"需要进一步说明吗\"等多余的话。"
        )
        user_msg = (
            f"请总结以下群聊记录（{time_str}内）：\n\n{chat_text}"
        )
        try:
            content = cloud_chat_completion(
                self.config,
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.7,
                max_tokens=1200,
            )
            content = content.strip()
            if not content:
                return "总结失败：模型返回为空"
            # Safety net: strip any think blocks
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            if not content:
                return "总结失败：模型返回为空"
            header = f"最近 {time_str} 群聊总结：\n"
            return header + content
        except Exception as e:
            logger.error(f"Ollama summary failed: {e}")
            return "总结失败：AI服务不可用，请稍后再试"
