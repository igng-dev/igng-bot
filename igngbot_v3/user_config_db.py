"""群聊配置数据库操作 — group_configs 表 CRUD"""
import logging
import aiomysql

from .config import Config

logger = logging.getLogger("yunying_chat.db")

DB_CONFIG = {
    "host": Config.DB_HOST,
    "port": Config.DB_PORT,
    "user": Config.DB_USER,
    "password": Config.DB_PASSWORD,
    "db": Config.DB_NAME,
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
                    is_chat_mode TINYINT(1) DEFAULT 0 COMMENT '聊天模式（跳过filter分析）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群聊配置表'
            """)


async def load_group_configs() -> dict[str, dict]:
    """加载所有群聊配置，返回 {group_id: {is_chat_mode, ...}}"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, is_chat_mode FROM group_configs"
            )
            rows = await cur.fetchall()
    result = {}
    for r in rows:
        gid = str(r[0])
        result[gid] = {
            "is_chat_mode": bool(r[1]),
        }
    return result


async def get_group_config(group_id: str) -> dict | None:
    """获取单个群聊配置"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT group_id, is_chat_mode FROM group_configs WHERE group_id = %s",
                (group_id,),
            )
            r = await cur.fetchone()
    if r:
        return {
            "is_chat_mode": bool(r[1]),
        }
    return None
