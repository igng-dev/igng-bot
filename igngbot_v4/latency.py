"""Mirror the durable local latency outbox into the account AI database.

Only timings, identifiers and a small allow-listed attribute set cross the
database boundary.  Prompt text, model output, tool arguments and message
content are intentionally not part of this contract.
"""
import json
import logging
import re
from datetime import datetime, timezone

from igngbot_shared import call_log_db as legacy


logger = logging.getLogger("yunying.latency")

_TRACE_RE = re.compile(r"^tr_[0-9a-f]{32,64}$")
_SAFE_ATTRIBUTE_KEYS = {
    "postType", "turn", "step", "attemptNo", "provider", "model", "requestModel",
    "success", "ttftMs", "firstOutputAt", "toolName", "callId", "isError",
    "httpStatus", "retcode", "messageSent", "sendStatus", "reason", "spanKind",
}


def _safe_attributes(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key not in _SAFE_ATTRIBUTE_KEYS:
            continue
        if isinstance(item, bool) or item is None or isinstance(item, (int, float, str)):
            text = item if not isinstance(item, str) else item[:191]
            result[key] = text
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return result if len(encoded) <= 4096 else {}


def _timestamp(value):
    """Normalize mysql2 JSON dates and numeric event times for aiomysql."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _span(value):
    if not isinstance(value, dict):
        return None
    span_id = str(value.get("span_id") or "")[:160]
    span_type = str(value.get("span_type") or "")[:64]
    started_at = _timestamp(value.get("started_at"))
    if not span_id or not span_type or started_at is None:
        return None
    trace_id = value.get("trace_id")
    trace_id = str(trace_id)[:64] if trace_id else None
    if trace_id and not _TRACE_RE.fullmatch(trace_id):
        trace_id = None
    return {
        "span_id": span_id,
        "trace_id": trace_id,
        "task_key": str(value.get("task_key") or "")[:191] or None,
        "correlation_id": str(value.get("correlation_id") or "")[:128] or None,
        "span_type": span_type,
        "parent_span_id": str(value.get("parent_span_id") or "")[:160] or None,
        "started_at": started_at,
        "ended_at": _timestamp(value.get("ended_at")),
        "status": str(value.get("status") or "ok")[:32],
        "attributes": _safe_attributes(value.get("attributes")),
    }


async def mirror_latency_spans(spans):
    """Upsert a batch from the bot-local outbox into the site AI database."""
    if not legacy.Config.SITE_AI_RECORDS_ENABLED:
        return {"ok": True, "mirrored": len(spans or []), "disabled": True}
    batch = [_span(item) for item in (spans if isinstance(spans, list) else [])]
    batch = [item for item in batch if item][:100]
    if not batch:
        return {"ok": True, "mirrored": 0}
    pool = await legacy._get_site_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                for item in batch:
                    await cur.execute(
                        """INSERT INTO ai_runtime_spans
                          (span_id,trace_id,task_key,correlation_id,span_type,parent_span_id,
                           started_at,ended_at,status,attributes)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                          ON DUPLICATE KEY UPDATE
                            trace_id=COALESCE(VALUES(trace_id),trace_id),
                            task_key=COALESCE(VALUES(task_key),task_key),
                            correlation_id=COALESCE(VALUES(correlation_id),correlation_id),
                            span_type=VALUES(span_type),
                            parent_span_id=COALESCE(VALUES(parent_span_id),parent_span_id),
                            started_at=LEAST(started_at,VALUES(started_at)),
                            ended_at=COALESCE(VALUES(ended_at),ended_at),
                            status=VALUES(status), attributes=COALESCE(VALUES(attributes),attributes),
                            updated_at=CURRENT_TIMESTAMP(3)""",
                        (
                            item["span_id"], item["trace_id"], item["task_key"], item["correlation_id"],
                            item["span_type"], item["parent_span_id"], item["started_at"], item["ended_at"],
                            item["status"], json.dumps(item["attributes"], ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                    if item["trace_id"] and item["task_key"]:
                        # Keep the explicit trace available to readers even if
                        # the span batch races the AI job mirror.
                        await cur.execute(
                            """UPDATE ai_jobs SET trace_id=COALESCE(trace_id,%s),strategy=JSON_SET(
                                IF(JSON_VALID(strategy),strategy,'{}'),'$.trace_id',%s)
                              WHERE service=%s AND task_key=%s""",
                            (item["trace_id"], item["trace_id"], legacy.SITE_AI_SERVICE, item["task_key"]),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
    return {"ok": True, "mirrored": len(batch)}
