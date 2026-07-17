"""IGNG 查询工具集 — MC服务器、玩家、领地、处罚、IGNg用户等查询

所有工具注册为 AstrBot llm_tool，供 LLM 调用。
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import aiomysql

from ..config import Config

logger = logging.getLogger("yunying_chat.tools")

# MC_STATUS DB (rm-2ze7v7v0evurv0953po)
MC_STATUS_DB = {
    "host": "rm-2ze7v7v0evurv0953po.mysql.rds.aliyuncs.com",
    "port": 3306,
    "user": "igng_bot",
    "password": Config.DB_PASSWORD,
    "db": "mc_status",
    "charset": "utf8mb4",
}

# MC_ACCOUNT DB (rm-2ze7v7v0evurv0953po)
MC_ACCOUNT_DB = {
    "host": "rm-2ze7v7v0evurv0953po.mysql.rds.aliyuncs.com",
    "port": 3306,
    "user": "igng_bot",
    "password": Config.DB_PASSWORD,
    "db": "mc_account",
    "charset": "utf8mb4",
}

# MC_LITEBANS DB (rm-2ze7v7v0evurv0953po)
MC_LITEBANS_DB = {
    "host": "rm-2ze7v7v0evurv0953po.mysql.rds.aliyuncs.com",
    "port": 3306,
    "user": "igng_bot",
    "password": Config.DB_PASSWORD,
    "db": "mc_litebans",
    "charset": "utf8mb4",
}

# IGNG_SITES DB (rm-rj94w0fari8g50ztf4o)
IGNG_SITES_DB = {
    "host": "rm-rj94w0fari8g50ztf4o.mysql.rds-aliyun-america.rds.aliyuncs.com",
    "port": 3306,
    "user": "igng_bot",
    "password": Config.DB_PASSWORD,
    "db": "igng_sites",
    "charset": "utf8mb4",
}

SERVER_ALIASES = {
    "clearx": "ClearX", "纯净": "ClearX", "纯净服": "ClearX",
    "ban": "Ban", "作弊": "Ban", "作弊服": "Ban",
    "lite-mods": "lite-mods", "轻量mod": "lite-mods", "轻量mod服": "lite-mods",
    "mods": "mods", "mod周目": "mods", "mod周目服": "mods",
    "mcservers": "mcservers", "老周目": "mcservers", "老周目集合服": "mcservers",
}


def _resolve_server_name(name: str) -> str | None:
    key = name.strip().lower()
    if key in SERVER_ALIASES:
        return SERVER_ALIASES[key]
    valid = {"clearx", "ban", "lite-mods", "mods", "mcservers"}
    if key in valid:
        return SERVER_ALIASES.get(key, key.capitalize())
    return None


# 持久化连接池 — 每个库一个池，避免反复创建/销毁
_pools: dict[str, aiomysql.Pool] = {}


async def _get_pool(cfg: dict) -> aiomysql.Pool:
    pool_key = f"{cfg['host']}:{cfg['port']}/{cfg['db']}"
    if pool_key not in _pools or _pools[pool_key]._closed:
        _pools[pool_key] = await aiomysql.create_pool(
            host=cfg["host"], port=cfg["port"],
            user=cfg["user"], password=cfg["password"],
            db=cfg["db"], charset=cfg["charset"],
            autocommit=True, maxsize=3, minsize=1,
        )
    return _pools[pool_key]


async def close_tool_pools():
    """关闭所有工具连接池（插件终止时调用）"""
    for key, pool in _pools.items():
        if not pool._closed:
            pool.close()
            await pool.wait_closed()
    _pools.clear()


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _utc_str_to_beijing(utc_str: str) -> str:
    """将 UTC 时间字符串转为北京时间 (UTC+8) 字符串"""
    if not utc_str:
        return utc_str
    try:
        utc_dt = datetime.strptime(str(utc_str)[:19], "%Y-%m-%d %H:%M:%S")
        beijing_dt = utc_dt + timedelta(hours=8)
        return beijing_dt.strftime("%Y-%m-%d %H:%M:%S") + " (北京时间)"
    except (ValueError, TypeError):
        return str(utc_str) + " (UTC)"


# ============================================================
#  工具 1: 获取服务器性能数据
# ============================================================

async def get_server_performance(server_name: str, range_hours: int = 24) -> str:
    """获取 MC 服务器性能数据（TPS、MSPT、CPU、内存、在线玩家数）

    Args:
        server_name(string): 服务器名（支持别名，如 clearx/ClearX/纯净/纯净服）
        range_hours(number): 查询范围（小时），最大 336（14天），默认 24
    """
    real_name = _resolve_server_name(server_name)
    if not real_name:
        return f"未知服务器: {server_name}"

    range_hours = max(1, min(range_hours, 336))
    pool = await _get_pool(MC_STATUS_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT server_id FROM servers WHERE server_name = %s",
                    (real_name,),
                )
                row = await cur.fetchone()
                if not row:
                    return f"服务器 {real_name} 不存在"
                sid = row[0]

                since = datetime.now(timezone.utc) - timedelta(hours=range_hours)
                since_str = since.strftime("%Y-%m-%d %H:%M:%S")

                bucket_seconds = max(300, int(range_hours * 3600 / 50))
                bucket_seconds = (bucket_seconds // 300) * 300
                interval_desc = f"{bucket_seconds // 60}分钟"

                sql = f"""
                    SELECT
                        FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(recorded_at) / {bucket_seconds}) * {bucket_seconds}) AS bucket,
                        ROUND(AVG(avg_tps), 2) AS avg_tps,
                        ROUND(AVG(avg_mspt), 2) AS avg_mspt,
                        ROUND(AVG(cpu_usage), 2) AS cpu_usage,
                        ROUND(AVG(memory_usage_mb), 0) AS memory_mb,
                        ROUND(AVG(online_players), 0) AS online_players
                    FROM server_performance_logs
                    WHERE server_id = %s AND recorded_at >= %s
                    GROUP BY bucket
                    ORDER BY bucket
                """
                await cur.execute(sql, (sid, since_str))
                rows = await cur.fetchall()

                data_points = []
                for r in rows:
                    data_points.append({
                        "time": _utc_str_to_beijing(str(r[0])),
                        "tps": float(r[1]) if r[1] else 0,
                        "mspt": float(r[2]) if r[2] else 0,
                        "cpu_pct": float(r[3]) if r[3] else 0,
                        "memory_mb": int(r[4]) if r[4] else 0,
                        "online_players": int(r[5]) if r[5] else 0,
                    })

                return json.dumps({
                    "success": True,
                    "server_name": real_name,
                    "range_hours": range_hours,
                    "granularity": interval_desc,
                    "data_points": data_points,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_server_performance error: %s", e)
        return f"查询失败: {e}"

# ============================================================
#  工具 2: 获取服务器延迟
# ============================================================

async def get_server_latency(server_name: str, range_hours: int = 24) -> str:
    """获取 MC 服务器各节点网络延迟（上海、日本、美国等）

    Args:
        server_name(string): 服务器名（支持别名）
        range_hours(number): 查询范围（小时），最大 336（14天），默认 24
    """
    real_name = _resolve_server_name(server_name)
    if not real_name:
        return f"未知服务器: {server_name}"

    range_hours = max(1, min(range_hours, 336))
    pool = await _get_pool(MC_STATUS_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT server_id FROM servers WHERE server_name = %s",
                    (real_name,),
                )
                row = await cur.fetchone()
                if not row:
                    return f"服务器 {real_name} 不存在"
                sid = row[0]

                await cur.execute("SELECT node_id, node_name FROM latency_nodes")
                nodes = {r[1]: r[0] for r in await cur.fetchall()}

                since = datetime.now(timezone.utc) - timedelta(hours=range_hours)
                since_str = since.strftime("%Y-%m-%d %H:%M:%S")

                results_by_node = {}
                bucket_seconds = max(60, int(range_hours * 3600 / 30))
                bucket_seconds = (bucket_seconds // 60) * 60

                for node_name, node_id in nodes.items():
                    sql = f"""
                        SELECT
                            FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(timestamp_utc) / {bucket_seconds}) * {bucket_seconds}) AS bucket,
                            ROUND(AVG(avg_latency_ms), 0) AS avg_lat,
                            ROUND(MAX(max_latency_ms), 0) AS max_lat,
                            ROUND(MIN(min_latency_ms), 0) AS min_lat,
                            ROUND(AVG(packet_loss_pct), 2) AS loss_pct
                        FROM latencies
                        WHERE server_id = %s AND node_id = %s AND timestamp_utc >= %s
                        GROUP BY bucket
                        ORDER BY bucket
                    """
                    await cur.execute(sql, (sid, node_id, since_str))
                    rows = await cur.fetchall()

                    node_data = []
                    for r in rows:
                        node_data.append({
                            "time": _utc_str_to_beijing(str(r[0])),
                            "avg_latency_ms": int(r[1]) if r[1] else 0,
                            "max_latency_ms": int(r[2]) if r[2] else 0,
                            "min_latency_ms": int(r[3]) if r[3] else 0,
                            "packet_loss_pct": float(r[4]) if r[4] else 0,
                        })
                    results_by_node[node_name] = {
                        "granularity": f"{bucket_seconds // 60}分钟",
                        "data_points": node_data,
                    }

                interval_desc = f"{bucket_seconds // 60}分钟"
                return json.dumps({
                    "success": True,
                    "server_name": real_name,
                    "range_hours": range_hours,
                    "granularity": interval_desc,
                    "nodes": results_by_node,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_server_latency error: %s", e)
        return f"查询失败: {e}"

# ============================================================
#  工具 3: 获取假人列表
# ============================================================

async def get_fake_players(server_name: str) -> str:
    """获取指定 MC 服务器上的所有假人信息（名字、世界、创建者、坐标、血量、饱食度）

    Args:
        server_name(string): 服务器名（支持别名）
    """
    real_name = _resolve_server_name(server_name)
    if not real_name:
        return f"未知服务器: {server_name}"

    pool = await _get_pool(MC_STATUS_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT server_id FROM servers WHERE server_name = %s",
                    (real_name,),
                )
                row = await cur.fetchone()
                if not row:
                    return f"服务器 {real_name} 不存在"
                sid = row[0]

                await cur.execute(
                    """SELECT fake_name, world, creator_name, x, y, z, health, hunger
                       FROM fake_player_list WHERE server_id = %s
                       ORDER BY fake_name""",
                    (sid,),
                )
                rows = await cur.fetchall()

                players = []
                for r in rows:
                    players.append({
                        "name": r[0],
                        "world": r[1],
                        "creator": r[2],
                        "coordinates": {"x": round(float(r[3]), 1), "y": round(float(r[4]), 1), "z": round(float(r[5]), 1)},
                        "health": float(r[6]) if r[6] else 0,
                        "hunger": int(r[7]) if r[7] else 0,
                    })

                return json.dumps({
                    "success": True,
                    "server_name": real_name,
                    "total_players": len(players),
                    "players": players,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_fake_players error: %s", e)
        return f"查询失败: {e}"

# ============================================================
#  工具 4: 查询玩家处罚记录
# ============================================================

async def query_player_punishments(player_names: str) -> str:
    """查询玩家在 MC 服务器上的处罚记录（封禁、禁言、警告等），支持同时查询多个玩家

    Args:
        player_names(string): 玩家名，多个用逗号分隔（如 \"player1,player2\"）
    """
    import re
    name_list = [n.strip() for n in re.split(r"[,，、\s]+", player_names.strip()) if n.strip()]
    if not name_list:
        return "请输入玩家名"

    seen = set()
    unique_names = []
    for n in name_list:
        key = n.lower()
        if key not in seen:
            seen.add(key)
            unique_names.append(n)

    pool = await _get_pool(MC_LITEBANS_DB)
    try:
        all_results = []
        not_found = []

        for player_name in unique_names:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    # 精确匹配
                    await cur.execute(
                        "SELECT DISTINCT name, uuid FROM litebans_history WHERE name = %s",
                        (player_name,),
                    )
                    players = await cur.fetchall()
                    if not players:
                        await cur.execute(
                            "SELECT DISTINCT name, uuid FROM litebans_history WHERE name LIKE %s",
                            (f"%{player_name}%",),
                        )
                        players = await cur.fetchall()

                    if not players:
                        not_found.append(player_name)
                        continue

                    for p in players:
                        uuid = p[1]
                        name = p[0]
                        bans = await _query_punishment_type(pool, uuid, "litebans_bans", "封禁")
                        mutes = await _query_punishment_type(pool, uuid, "litebans_mutes", "禁言")
                        warnings = await _query_warnings(pool, uuid)

                        if bans or mutes or warnings:
                            all_results.append({
                                "name": name,
                                "uuid": uuid,
                                "total_records": len(bans) + len(mutes) + len(warnings),
                                "active_bans": sum(1 for b in bans if b["active"]),
                                "active_mutes": sum(1 for m in mutes if m["active"]),
                                "bans": bans,
                                "mutes": mutes,
                                "warnings": warnings,
                            })
                        else:
                            all_results.append({
                                "name": name,
                                "uuid": uuid,
                                "total_records": 0,
                                "message": "无处罚记录",
                            })

        parts = []
        if all_results:
            found_names = [r["name"] for r in all_results]
            parts.append(f"查询了 {len(found_names)} 名玩家：{'、'.join(found_names)}")
        if not_found:
            parts.append(f"未找到玩家：{'、'.join(not_found)}")

        return json.dumps({
            "success": True,
            "message": "；".join(parts) if parts else "无结果",
            "data": all_results,
        }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("query_player_punishments error: %s", e)
        return f"查询失败: {e}"

async def _query_punishment_type(pool, uuid: str, table: str, type_label: str) -> list[dict]:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT reason, banned_by_name, time, until, server_scope, server_origin,
                           active, removed_by_name, removed_by_reason
                    FROM {table} WHERE uuid = %s ORDER BY time DESC""",
                (uuid,),
            )
            rows = await cur.fetchall()
            results = []
            for r in rows:
                until_ts = r[3] if isinstance(r[3], int) else 0
                results.append({
                    "type": type_label,
                    "reason": r[0] or "",
                    "operator": r[1] or "",
                    "time": _ms_to_dt(r[2]),
                    "expires": "永久" if until_ts <= 0 else _ms_to_dt(until_ts),
                    "server_scope": r[4] or "*",
                    "server_origin": r[5] or "",
                    "active": bool(r[6]),
                    "status": "进行中" if bool(r[6]) else "已过期/已解除",
                    "unban_by": r[7] or "",
                    "unban_reason": r[8] or "",
                })
            return results


async def _query_warnings(pool, uuid: str) -> list[dict]:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT reason, banned_by_name, time, until, server_scope, server_origin,
                          active, warned
                   FROM litebans_warnings WHERE uuid = %s ORDER BY time DESC""",
                (uuid,),
            )
            rows = await cur.fetchall()
            results = []
            for r in rows:
                results.append({
                    "type": "警告",
                    "reason": r[0] or "",
                    "operator": r[1] or "",
                    "time": _ms_to_dt(r[2]),
                    "until": _ms_to_dt(r[3]),
                    "server_scope": r[4] or "*",
                    "server_origin": r[5] or "",
                    "active": bool(r[6]),
                    "warned": bool(r[7]),
                })
            return results


def _ms_to_dt(ms_timestamp: int) -> str:
    if ms_timestamp <= 0:
        return "永久"
    try:
        dt = datetime.fromtimestamp(ms_timestamp / 1000, tz=timezone.utc)
        # 转为北京时间 (UTC+8)
        beijing_dt = dt + timedelta(hours=8)
        return beijing_dt.strftime("%Y-%m-%d %H:%M:%S") + " (北京时间)"
    except (OSError, ValueError, OverflowError):
        return str(ms_timestamp)


# ============================================================
#  工具 5: 查询我的假人
# ============================================================

async def get_my_fake_players(message_event: Any = None) -> str:
    """查询自己名下的全部假人信息（不是所有人的假人，只查调用者自己的）

    Args:
        message_event: AstrBot 消息事件，用于获取调用者 QQ 号
    """
    return "请通过 @我 的方式查询，以便系统识别你的 QQ 号"


# ============================================================
#  工具 6: 查询我的领地
# ============================================================

async def get_my_lands(message_event: Any = None) -> str:
    """查询自己名下的全部领地（包括公开和私有的），不需要参数

    Args:
        message_event: AstrBot 消息事件，用于获取调用者 QQ 号
    """
    return "请通过 @我 的方式查询，以便系统识别你的 QQ 号"


# ============================================================
#  工具 7: 查询公开领地
# ============================================================

async def get_public_lands(player_name: str) -> str:
    """查询某玩家的公开领地列表（领地名、服务器、世界名）

    Args:
        player_name(string): 玩家游戏名
    """
    pool = await _get_pool(MC_ACCOUNT_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT land_name, server_id, world, x, y, z
                       FROM land_records
                       WHERE owner_name = %s AND is_public = 1
                       ORDER BY land_name""",
                    (player_name,),
                )
                rows = await cur.fetchall()

                if not rows:
                    return json.dumps({
                        "success": True,
                        "found": False,
                        "message": f"玩家「{player_name}」没有公开领地",
                        "lands": [],
                    }, ensure_ascii=False)

                # 获取服务器名
                await cur.execute("SELECT server_id, server_name FROM servers")
                server_map = {r[0]: r[1] for r in await cur.fetchall()}

                lands = []
                for r in rows:
                    server_name = server_map.get(r[1], str(r[1]))
                    lands.append({
                        "land_name": r[0],
                        "server": server_name,
                        "world": r[2],
                        "coordinates": {"x": round(float(r[3]), 1), "y": round(float(r[4]), 1), "z": round(float(r[5]), 1)},
                    })

                return json.dumps({
                    "success": True,
                    "found": True,
                    "message": f"玩家「{player_name}」共有 {len(lands)} 个公开领地",
                    "player": player_name,
                    "total": len(lands),
                    "lands": lands,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_public_lands error: %s", e)
        return f"查询失败: {e}"

# ============================================================
#  工具 7: 查询我的领地详情
# ============================================================

async def get_my_land_detail(server_name: str, land_name: str, message_event: Any = None) -> str:
    """查询自己名下某个服务器的某个领地的详细信息

    Args:
        server_name(string): 服务器名（支持别名）
        land_name(string): 领地名称
        message_event: AstrBot 消息事件，用于获取调用者 QQ 号
    """
    return "请通过 @我 的方式查询，以便系统识别你的 QQ 号"


# ============================================================
#  工具 8: 查询 IGNg 用户信息
# ============================================================

async def query_igng_user_info(qq_or_username: str) -> str:
    """查询 IGNg 网站用户信息（等级、经验值、贴子数、评论数）

    Args:
        qq_or_username(string): QQ 号或 IGNg 用户名
    """
    if not qq_or_username or not qq_or_username.strip():
        return "请输入 QQ 号或用户名"

    qq_or_username = qq_or_username.strip()
    pool = await _get_pool(IGNG_SITES_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                user_id = None
                resolved_method = ""

                if qq_or_username.isdigit():
                    await cur.execute(
                        "SELECT user_id FROM user_qqs WHERE qq_number = %s",
                        (qq_or_username,),
                    )
                    row = await cur.fetchone()
                    if row:
                        user_id = row[0]
                        resolved_method = "QQ"
                else:
                    await cur.execute(
                        "SELECT id FROM users WHERE username = %s",
                        (qq_or_username,),
                    )
                    row = await cur.fetchone()
                    if row:
                        user_id = row[0]
                        resolved_method = "用户名"

                if not user_id:
                    return json.dumps({
                        "success": True,
                        "found": False,
                        "message": f"未找到用户「{qq_or_username}」",
                    }, ensure_ascii=False)

                await cur.execute(
                    "SELECT username, nickname, email, status FROM users WHERE id = %s",
                    (user_id,),
                )
                user_row = await cur.fetchone()
                if not user_row:
                    return json.dumps({"success": False, "error": "用户数据异常"}, ensure_ascii=False)

                username, nickname, email, status = user_row

                await cur.execute(
                    "SELECT exp FROM igng_user_exp WHERE user_id = %s",
                    (user_id,),
                )
                exp_row = await cur.fetchone()
                exp = exp_row[0] if exp_row else 0
                level = exp // 100 + 1

                await cur.execute("SELECT COUNT(*) FROM posts WHERE user_id = %s", (user_id,))
                post_count = (await cur.fetchone())[0]

                await cur.execute("SELECT COUNT(*) FROM comments WHERE user_id = %s", (user_id,))
                comment_count = (await cur.fetchone())[0]

                return json.dumps({
                    "success": True,
                    "found": True,
                    "user_info": {
                        "user_id": user_id,
                        "username": username,
                        "nickname": nickname or "",
                        "email": email or "",
                        "status": status,
                        "level": level,
                        "experience": exp,
                        "next_level_exp": (level * 100) - exp,
                        "total_posts": post_count,
                        "total_comments": comment_count,
                    },
                    "resolved_by": resolved_method,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("query_igng_user_info error: %s", e)
        return f"查询失败: {e}"

# ============================================================
#  工具 9: 查询文章详情
# ============================================================

async def get_post_detail(post_id: int) -> str:
    """查询 IGNg 网站某篇文章的完整信息（标题、内容、作者、分类、标签等）

    Args:
        post_id(number): 文章 ID
    """
    pool = await _get_pool(IGNG_SITES_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                # 第一步：只查状态字段，判断文章是否存在及可访问性
                await cur.execute(
                    "SELECT post_id, title, status, ban_status FROM posts WHERE post_id = %s",
                    (post_id,),
                )
                check_row = await cur.fetchone()

                if not check_row:
                    return json.dumps({
                        "success": True,
                        "found": False,
                        "message": f"文章不存在（ID: {post_id}）",
                    }, ensure_ascii=False)

                post_status = check_row[2]
                post_ban = check_row[3]

                # status: 0=已发布，非0=未发布（草稿/审核等）
                if post_status != 0:
                    return json.dumps({
                        "success": True,
                        "found": False,
                        "message": f"文章 ID {post_id} 无法查看：文章未发布",
                    }, ensure_ascii=False)

                # ban_status: 0/1=正常，其他=禁止查看
                if post_ban not in (0, 1):
                    return json.dumps({
                        "success": True,
                        "found": False,
                        "message": f"文章 ID {post_id} 无法查看：文章已被禁止查看",
                    }, ensure_ascii=False)

                # 第二步：文章可访问，查完整信息
                await cur.execute(
                    """SELECT p.post_id, p.title, p.excerpt, p.content,
                              p.views, p.comment_count, p.like_count, p.hot_score,
                              p.status, p.tags, p.is_bot,
                              p.created_at, p.updated_at, p.column_id,
                              u.id, u.username, u.nickname,
                              c.category_id, c.name
                       FROM posts p
                       LEFT JOIN users u ON p.user_id = u.id
                       LEFT JOIN categories c ON p.category_id = c.category_id
                       WHERE p.post_id = %s""",
                    (post_id,),
                )
                row = await cur.fetchone()

                if not row:
                    return json.dumps({
                        "success": True,
                        "found": False,
                        "message": f"文章不存在（ID: {post_id}）",
                    }, ensure_ascii=False)

                tags_list = []
                if row[9]:
                    try:
                        tags_list = json.loads(row[9])
                        if isinstance(tags_list, str):
                            tags_list = [t.strip() for t in tags_list.split(",") if t.strip()]
                    except (json.JSONDecodeError, TypeError):
                        tags_list = [t.strip() for t in str(row[9]).split(",") if t.strip()]

                return json.dumps({
                    "success": True,
                    "found": True,
                    "post": {
                        "id": row[0],
                        "title": row[1],
                        "excerpt": row[2] or "",
                        "content": row[3] or "",
                        "views": row[4],
                        "comment_count": row[5],
                        "like_count": row[6],
                        "hot_score": float(row[7]) if row[7] else 0,
                        "status": row[8],
                        "tags": tags_list,
                        "is_bot": bool(row[10]),
                        "created_at": _utc_str_to_beijing(str(row[11])),
                        "updated_at": _utc_str_to_beijing(str(row[12])),
                        "column_id": row[13],
                        "author": {
                            "id": row[14],
                            "username": row[15],
                            "nickname": row[16] or "",
                        },
                        "category": {
                            "id": row[17],
                            "name": row[18],
                        } if row[17] else None,
                    },
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_post_detail error: %s", e)
        return f"查询失败: {e}"

# ============================================================
#  QQ 账号解析（内部使用，非工具）
# ============================================================

async def resolve_qq_to_usernames(qq_number: str) -> list[str]:
    """通过 QQ 号查询绑定的 MC 游戏名"""
    pool = await _get_pool(IGNG_SITES_DB)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id FROM user_qqs WHERE qq_number = %s",
                (qq_number,),
            )
            row = await cur.fetchone()
            if not row:
                return []
            user_id = row[0]
            await cur.execute(
                "SELECT username FROM users WHERE id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
            platform_username = row[0] if row else ""

    usernames = [platform_username] if platform_username else []
    pool2 = await _get_pool(MC_ACCOUNT_DB)
    async with pool2.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT mc_username FROM mc_mapping WHERE igng_id = %s",
                (user_id,),
            )
            async for r in cur:
                if r[0] not in usernames:
                    usernames.append(r[0])

    return usernames


async def get_my_fake_players_by_qq(qq_number: str) -> str:
    """通过 QQ 号查询自己的假人"""
    usernames = await resolve_qq_to_usernames(qq_number)
    if not usernames:
        return "你尚未绑定 IGNG 账号，请先在 IGNG 网站绑定 QQ"

    pool = await _get_pool(MC_STATUS_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                all_fakes = []
                for username in usernames:
                    await cur.execute(
                        """SELECT fake_name, world, creator_name, x, y, z, health, hunger, server_id
                           FROM fake_player_list WHERE creator_name = %s
                           ORDER BY fake_name""",
                        (username,),
                    )
                    rows = await cur.fetchall()
                    for r in rows:
                        sid = r[8]
                        await cur.execute(
                            "SELECT server_name FROM servers WHERE server_id = %s",
                            (sid,),
                        )
                        srv_row = await cur.fetchone()
                        all_fakes.append({
                            "fake_name": r[0],
                            "world": r[1],
                            "creator": r[2],
                            "coordinates": {"x": round(float(r[3]), 1), "y": round(float(r[4]), 1), "z": round(float(r[5]), 1)},
                            "health": float(r[6]) if r[6] else 0,
                            "hunger": int(r[7]) if r[7] else 0,
                            "server": srv_row[0] if srv_row else str(sid),
                        })

                return json.dumps({
                    "success": True,
                    "message": f"共找到 {len(all_fakes)} 个假人（游戏账号：{'、'.join(usernames)}）",
                    "accounts": usernames,
                    "total": len(all_fakes),
                    "fake_players": all_fakes,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_my_fake_players_by_qq error: %s", e)
        return f"查询失败: {e}"

async def get_my_lands_by_qq(qq_number: str) -> str:
    """通过 QQ 号查询自己的领地"""
    usernames = await resolve_qq_to_usernames(qq_number)
    if not usernames:
        return "你尚未绑定 IGNG 账号，请先在 IGNG 网站绑定 QQ"

    pool = await _get_pool(MC_ACCOUNT_DB)
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                all_lands = []
                for username in usernames:
                    await cur.execute(
                        """SELECT land_name, server_id, world, x, y, z,
                                  is_public, description, plugin_type, last_updated
                           FROM land_records WHERE owner_name = %s
                           ORDER BY server_id, land_name""",
                        (username,),
                    )
                    rows = await cur.fetchall()
                    for r in rows:
                        await cur.execute(
                            "SELECT server_name FROM servers WHERE server_id = %s",
                            (r[1],),
                        )
                        srv_row = await cur.fetchone()
                        server_name = srv_row[0] if srv_row else str(r[1])
                        all_lands.append({
                            "land_name": r[0],
                            "server": server_name,
                            "world": r[2],
                            "coordinates": {"x": round(float(r[3]), 1), "y": round(float(r[4]), 1), "z": round(float(r[5]), 1)},
                            "is_public": bool(r[6]),
                            "description": r[7] or "",
                            "plugin_type": r[8],
                            "last_updated": _utc_str_to_beijing(str(r[9])),
                            "owner": username,
                        })

                return json.dumps({
                    "success": True,
                    "message": f"共找到 {len(all_lands)} 个领地（游戏账号：{'、'.join(usernames)}）",
                    "accounts": usernames,
                    "total": len(all_lands),
                    "lands": all_lands,
                }, ensure_ascii=False, default=_json_default)
    except Exception as e:
        logger.error("get_my_lands_by_qq error: %s", e)
        return f"查询失败: {e}"


# ============================================================
#  表情包数据加载（启动时调用一次）
# ============================================================

# IGNG_BOT DB（与 call_logs/user_config 同一个库）
IGNG_BOT_DB = {
    "host": "rm-2ze7v7v0evurv0953po.mysql.rds.aliyuncs.com",
    "port": 3306,
    "user": "igng_bot",
    "password": Config.DB_PASSWORD,
    "db": "igng_bot",
    "charset": "utf8mb4",
}


async def load_stickers() -> list[dict]:
    """从 igng_bot.stickers 加载所有表情包数据（启动时调用一次）"""
    conn = await aiomysql.connect(
        host=IGNG_BOT_DB["host"], port=IGNG_BOT_DB["port"],
        user=IGNG_BOT_DB["user"], password=IGNG_BOT_DB["password"],
        db=IGNG_BOT_DB["db"], charset=IGNG_BOT_DB["charset"],
        autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name, image_url, file_path, avatar_id FROM stickers ORDER BY name, id"
            )
            rows = await cur.fetchall()
            stickers = []
            for r in rows:
                stickers.append({
                    "id": r[0],
                    "name": r[1],
                    "image_url": r[2],
                    "file_path": r[3],
                    "avatar_id": r[4],
                })
            return stickers
    finally:
        conn.close()
