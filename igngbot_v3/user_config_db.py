"""用户配置数据库操作 — user_config 表 CRUD"""
import logging
import aiomysql

from .config import Config

logger = logging.getLogger("yunying_chat.db")

DB_CONFIG = {
    "host": "db.example.internal",
    "port": 3306,
    "user": "igng_bot",
    "password": Config.DB_PASSWORD,
    "db": "igng_bot",
    "charset": "utf8mb4",
}

_pool: aiomysql.Pool | None = None

async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            db=DB_CONFIG["db"],
            charset=DB_CONFIG["charset"],
            autocommit=True,
            maxsize=5,
            minsize=1,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def ensure_user_config_table():
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id VARCHAR(32) NOT NULL PRIMARY KEY,
                    affinity_value INT DEFAULT 50,
                    affinity_enabled TINYINT(1) DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )


async def get_user_config(user_id: str) -> dict:
    """获取用户完整配置，不存在则创建默认并返回"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT user_id, affinity_value, affinity_enabled
                   FROM user_config WHERE user_id = %s""",
                (user_id,),
            )
            row = await cur.fetchone()
            if row:
                return {
                    "user_id": str(row[0]),
                    "affinity_value": row[1],
                    "affinity_enabled": bool(row[2]),
                }
            # 创建默认记录
            await cur.execute(
                """INSERT IGNORE INTO user_config
                   (user_id, affinity_value, affinity_enabled)
                   VALUES (%s, 50, 1)""",
                (user_id,),
            )
            return {
                "user_id": user_id,
                "affinity_value": 50,
                "affinity_enabled": True,
            }


async def get_user_configs_batch(user_ids: list[str]) -> dict[str, dict]:
    """批量获取多个用户的完整配置"""
    if not user_ids:
        return {}
    pool = await get_pool()
    placeholders = ",".join("%s" for _ in user_ids)
    sql = f"""SELECT user_id, affinity_value, affinity_enabled
              FROM user_config WHERE user_id IN ({placeholders})"""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, user_ids)
            rows = await cur.fetchall()
    result = {}
    for r in rows:
        result[str(r[0])] = {
            "user_id": str(r[0]),
            "affinity_value": r[1],
            "affinity_enabled": bool(r[2]),
        }
    # 未设置的补默认
    for uid in user_ids:
        if uid not in result:
            await _insert_default(uid)
            result[uid] = {
                "user_id": uid,
                "affinity_value": 50,
                "affinity_enabled": True,
            }
    return result


async def _insert_default(user_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT IGNORE INTO user_config
                   (user_id, affinity_value, affinity_enabled)
                   VALUES (%s, 50, 1)""",
                (user_id,),
            )


async def upsert_affinity(user_id: str, affinity_value: int):
    """更新好感度"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO user_config (user_id, affinity_value, affinity_enabled)
                   VALUES (%s, %s, 1)
                   ON DUPLICATE KEY UPDATE affinity_value = %s""",
                (user_id, affinity_value, affinity_value),
            )


# ============================================================
#  群聊配置 (group_configs 表)
# ============================================================


async def ensure_group_configs_table():
    """确保 group_configs 表存在"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS group_configs (
                    group_id BIGINT NOT NULL PRIMARY KEY COMMENT '群号',
                    group_name VARCHAR(255) DEFAULT '' COMMENT '群名称',
                    is_content_review TINYINT(1) DEFAULT 0 COMMENT '内容审核标记',
                    is_chat_mode TINYINT(1) DEFAULT 0 COMMENT '聊天模式（跳过filter分析）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群聊配置表'
            """)


async def load_group_configs() -> dict[str, dict]:
    """加载所有群聊配置，返回 {group_id: {is_chat_mode, is_content_review, ...}}"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, is_chat_mode, is_content_review FROM group_configs"
            )
            rows = await cur.fetchall()
    result = {}
    for r in rows:
        gid = str(r[0])
        result[gid] = {
            "is_chat_mode": bool(r[1]),
            "is_content_review": bool(r[2]),
        }
    return result


async def get_group_config(group_id: str) -> dict | None:
    """获取单个群聊配置"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, is_chat_mode, is_content_review FROM group_configs WHERE group_id = %s",
                (group_id,),
            )
            r = await cur.fetchone()
    if r:
        return {
            "is_chat_mode": bool(r[1]),
            "is_content_review": bool(r[2]),
        }
    return None
