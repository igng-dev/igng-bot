"""LLM 调用日志存储 — call_logs 表"""

import json
import logging
from datetime import datetime

import aiomysql

from .user_config_db import DB_CONFIG

logger = logging.getLogger("yunying_chat")

_pool = None


async def _get_pool():
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            db=DB_CONFIG["db"],
            charset="utf8mb4",
            autocommit=True,
        )
    return _pool


async def ensure_call_logs_table():
    """创建 call_logs 表（如果不存在）"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """CREATE TABLE IF NOT EXISTS call_logs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    group_id VARCHAR(50) NOT NULL DEFAULT '',
                    sender_id VARCHAR(50) NOT NULL DEFAULT '',
                    task_id BIGINT DEFAULT NULL,
                    sender_name VARCHAR(100) DEFAULT '',
                    message_text TEXT,
                    call_type VARCHAR(20) NOT NULL COMMENT 'filter=消息过滤, agent=正式回复',
                    model VARCHAR(50) DEFAULT '',
                    system_prompt TEXT,
                    user_prompt TEXT,
                    thinking_content MEDIUMTEXT COMMENT '思考过程',
                    response_content TEXT COMMENT '回复/判断内容',
                    tool_calls JSON COMMENT '工具调用记录',
                    token_usage JSON COMMENT 'token用量',
                    duration_ms INT DEFAULT 0 COMMENT '调用耗时',
                    success TINYINT(1) DEFAULT 1,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_group_time (group_id, created_at),
                    INDEX idx_sender (sender_id),
                    INDEX idx_task (task_id),
                    INDEX idx_call_type (call_type),
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            try:
                await cur.execute(
                    "ALTER TABLE call_logs ADD COLUMN task_id BIGINT DEFAULT NULL AFTER sender_id"
                )
            except Exception:
                pass
            try:
                await cur.execute(
                    "ALTER TABLE call_logs ADD INDEX idx_task (task_id)"
                )
            except Exception:
                pass
    logger.info("[云萤] call_logs 表已就绪")


async def insert_call_log(
    group_id: str = "",
    sender_id: str = "",
    task_id: int | None = None,
    sender_name: str = "",
    message_text: str = "",
    call_type: str = "filter",
    model: str = "",
    system_prompt: str = "",
    user_prompt: str = "",
    thinking_content: str = "",
    response_content: str = "",
    tool_calls: list = None,
    token_usage: dict = None,
    duration_ms: int = 0,
    success: bool = True,
    error_message: str = "",
) -> int:
    """插入一条调用日志，返回自增 ID"""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO call_logs
                   (group_id, sender_id, task_id, sender_name, message_text, call_type, model,
                    system_prompt, user_prompt, thinking_content, response_content,
                    tool_calls, token_usage, duration_ms, success, error_message)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(group_id),
                    str(sender_id),
                    task_id,
                    sender_name,
                    message_text,
                    call_type,
                    model,
                    system_prompt[:65535] if system_prompt else "",
                    user_prompt[:65535] if user_prompt else "",
                    thinking_content,
                    response_content[:65535] if response_content else "",
                    (
                        tool_calls
                        if isinstance(tool_calls, str)
                        else json.dumps(tool_calls, ensure_ascii=False)
                        if tool_calls
                        else None
                    ),
                    (
                        token_usage
                        if isinstance(token_usage, str)
                        else json.dumps(token_usage, ensure_ascii=False)
                        if token_usage
                        else None
                    ),
                    duration_ms,
                    1 if success else 0,
                    error_message[:2000] if error_message else "",
                ),
            )
            return cur.lastrowid


async def close_call_log_pool():
    global _pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
