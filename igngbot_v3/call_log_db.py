"""LLM 调用日志存储 — call_logs 表，并把每次调用镜像到站点 AI 记录库
(ai_jobs / ai_job_attempts)。"""

import json
import logging
from datetime import datetime, timedelta

import aiomysql

from .config import Config
from .user_config_db import DB_CONFIG

logger = logging.getLogger("yunying_chat")

SITE_AI_DB_CONFIG = {
    "host": Config.SITE_AI_DB_HOST,
    "port": Config.SITE_AI_DB_PORT,
    "user": Config.SITE_AI_DB_USER,
    "password": Config.SITE_AI_DB_PASSWORD,
    "db": Config.SITE_AI_DB_NAME,
    "charset": "utf8mb4",
}
SITE_AI_SERVICE = "igng-bot"
SITE_AI_OPERATOR_TYPE = "BOT"

_pool = None
_site_pool = None


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


def _extract_tokens(token_usage: dict | None) -> tuple[int, int, int, int]:
    """从 OpenAI 风格 usage 中解析 (prompt, completion, total, cached) token。"""
    usage = token_usage if isinstance(token_usage, dict) else {}
    if "inputTokens" in usage:
        cached = int(usage.get("cacheReadTokens") or 0) + int(usage.get("cacheWriteTokens") or 0)
        prompt = int(usage.get("inputTokens") or 0) + cached
        completion = int(usage.get("outputTokens") or 0)
        return prompt, completion, int(usage.get("totalTokens") or prompt + completion), cached
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    cached = 0
    cache_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(cache_details, dict):
        cached += int(cache_details.get("cached_tokens") or 0)
    cached += int(usage.get("prompt_cache_hit_tokens") or 0)
    return prompt, completion, total, cached


async def _get_site_pool():
    global _site_pool
    if _site_pool is None:
        _site_pool = await aiomysql.create_pool(
            host=SITE_AI_DB_CONFIG["host"],
            port=SITE_AI_DB_CONFIG["port"],
            user=SITE_AI_DB_CONFIG["user"],
            password=SITE_AI_DB_CONFIG["password"],
            db=SITE_AI_DB_CONFIG["db"],
            charset=SITE_AI_DB_CONFIG["charset"],
            autocommit=True,
            maxsize=3,
            minsize=1,
        )
    return _site_pool


async def mirror_call_to_site(
    *,
    call_log_id: int | None,
    group_id: str = "",
    sender_id: str = "",
    sender_name: str = "",
    message_text: str = "",
    call_type: str = "chat",
    model: str = "",
    system_prompt: str = "",
    user_prompt: str = "",
    response_content: str = "",
    token_usage: dict = None,
    duration_ms: int = 0,
    success: bool = True,
    error_message: str = "",
    provider: str = "local",
    durable: bool = False,
) -> bool:
    """Mirror idempotently. V3 callers may ignore failure; V4 durable callers retry."""
    if not Config.SITE_AI_RECORDS_ENABLED:
        return True
    if call_log_id is None:
        return False
    try:
        prompt_tokens, completion_tokens, total_tokens, cached_tokens = _extract_tokens(token_usage)
        ended_at = datetime.now()
        started_at = ended_at - timedelta(milliseconds=max(0, int(duration_ms or 0)))
        strategy = json.dumps({"bot_call_log_id": call_log_id, "group_id": str(group_id or ""),
            "sender_id": str(sender_id or ""), "sender_name": sender_name or "",
            "message_text": (message_text or "")[:2000], "duration_ms": int(duration_ms or 0)}, ensure_ascii=False)
        pool = await _get_site_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                lock_name = f"igngbot-site-call:{call_log_id}"
                await cur.execute("SELECT GET_LOCK(%s,10)", (lock_name,))
                if (await cur.fetchone())[0] != 1:
                    raise RuntimeError("site call mirror locked")
                try:
                    await conn.begin()
                    await cur.execute("SELECT id FROM ai_jobs WHERE service=%s AND task_key=%s AND task_type=%s LIMIT 1", (SITE_AI_SERVICE, str(call_log_id), call_type))
                    existing = await cur.fetchone()
                    if existing:
                        job_id = existing[0]
                    else:
                        await cur.execute(
                            """INSERT INTO ai_jobs
                               (service, task_type, task_key, operator_type, operator_id,
                                strategy, system_prompt, user_prompt, status, attempt_count,
                                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                                round, last_error, final_result)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (
                                SITE_AI_SERVICE,
                                call_type,
                                str(call_log_id),
                                SITE_AI_OPERATOR_TYPE,
                                None,
                                strategy,
                                system_prompt or None,
                                user_prompt or None,
                                "success" if success else "failed",
                                1,
                                prompt_tokens,
                                completion_tokens,
                                total_tokens,
                                cached_tokens,
                                0,
                                error_message[:2000] if error_message else None,
                                response_content or None,
                            ),
                        )
                        job_id = cur.lastrowid
                    await cur.execute("SELECT id FROM ai_job_attempts WHERE job_id=%s AND attempt_no=1 AND round=0 LIMIT 1", (job_id,))
                    if not await cur.fetchone():
                        await cur.execute(
                            """INSERT INTO ai_job_attempts
                               (job_id, provider, model, attempt_no, round, is_fallback,
                                started_at, ended_at, ok, prompt_tokens, completion_tokens,
                                total_tokens, cached_tokens, error_message, raw_response,
                                selected)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (
                                job_id,
                                provider or "unknown",
                                model or None,
                                1,
                                0,
                                0,
                                started_at,
                                ended_at,
                                1 if success else 0,
                                prompt_tokens,
                                completion_tokens,
                                total_tokens,
                                cached_tokens,
                                error_message[:2000] if error_message else None,
                                response_content or None,
                                1,
                            ),
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                finally:
                    await cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        return True
    except Exception:
        logger.exception("[云萤] 站点 AI 记录镜像写入失败 call_log_id=%s", call_log_id)
        if durable:
            raise
        return False


async def close_call_log_pool():
    global _pool, _site_pool
    if _pool:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
    if _site_pool:
        _site_pool.close()
        await _site_pool.wait_closed()
        _site_pool = None
