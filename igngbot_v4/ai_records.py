"""Native DSH task/attempt export to the existing website AI tables.

The bot outbox and official Session are durable inputs. This adapter owns only SQL
accounting, never model calls. V3's legacy mirroring remains a rollback adapter.

Token buckets follow tokscale's DSH parser (uncached input, cache read/write,
reasoning carved out of output) and the cost is the new-api real bill: the
gateway's log quota when the request is matched, otherwise its settlement
formula over the same ratios. Missing usage or an unpriced model stays NULL.
"""
import hashlib
import json
from datetime import datetime, timezone

from igngbot_shared import call_log_db as legacy
from . import newapi_billing


_NATIVE_BREAKDOWN_KEYS = ("input", "output", "cacheRead", "cacheWrite", "reasoning", "prompt", "completion", "total", "cached")


def tokens(record):
    """Return the five-bucket breakdown, or None when the provider reported nothing.

    The live Node accounting writes camelCase buckets; historical records carry
    raw provider usage. Both are normalized to the same snake_case shape here.
    """
    native = record.get("token_breakdown")
    if isinstance(native, dict) and all(key in native for key in _NATIVE_BREAKDOWN_KEYS):
        return {
            "input_tokens": int(native["input"] or 0),
            "output_tokens": int(native["output"] or 0),
            "cache_read_tokens": int(native["cacheRead"] or 0),
            "cache_write_tokens": int(native["cacheWrite"] or 0),
            "reasoning_tokens": int(native["reasoning"] or 0),
            "prompt_tokens": int(native["prompt"] or 0),
            "completion_tokens": int(native["completion"] or 0),
            "total_tokens": int(native["total"] or 0),
            "cached_tokens": int(native["cached"] or 0),
        }
    return legacy._token_breakdown(record.get("token_usage"))


def timestamp(value):
    return datetime.fromtimestamp(float(value) / 1000, timezone.utc).replace(tzinfo=None) if value is not None else datetime.now(timezone.utc).replace(tzinfo=None)


def _legacy_tokens(breakdown):
    if not breakdown:
        return None, None, None, None
    return (breakdown["prompt_tokens"], breakdown["completion_tokens"],
            breakdown["total_tokens"], breakdown["cached_tokens"])


async def mirror_native_record(record_id, record):
    if not legacy.Config.SITE_AI_RECORDS_ENABLED:
        return True
    # Normalize tokens and resolve the real bill before taking the task lock:
    # the new-api lookup is cached but may still be network I/O, and the job row
    # must not stay locked across it.
    breakdown = tokens(record) if record["record_kind"] == "attempt" else None
    cost = await newapi_billing.cost_for(breakdown, record.get("model"), record.get("ended_at"), record.get("request_model")) if breakdown else None
    task_key = record["task_key"]
    lock = "yunying-ai:" + hashlib.sha256(task_key.encode()).hexdigest()[:48]
    pool = await legacy._get_site_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s,10)", (lock,))
            if (await cur.fetchone())[0] != 1:
                raise RuntimeError("website accounting lock unavailable")
            try:
                await conn.begin()
                await cur.execute("SELECT id,strategy FROM ai_jobs WHERE service=%s AND task_key=%s FOR UPDATE", (legacy.SITE_AI_SERVICE, task_key))
                prior = await cur.fetchone()
                # Existing pre-adjustment jobs remain untouched on native-history replay.
                # A task closer without a new-version attempt never creates an empty job.
                if not prior and record["record_kind"] == "task-end":
                    await conn.commit()
                    return True
                if not prior:
                    strategy = {"dsh_session_id": record_id.rsplit(":", 1)[0], "native_turn": record.get("native_turn"),
                                "group_id": record.get("group_id"), "sender_id": record.get("sender_id"),
                                "sender_name": record.get("sender_name"), "message_text": record.get("message_text", "")[:2000],
                                "accounting_version": 3, "usage_source": "provider"}
                    # Historian runs share this exact accounting pipeline, including compaction,
                    # settlement replacement, NULL usage and new-api pricing.
                    for key in ("report_run_id", "report_id", "phase"):
                        if record.get(key) is not None:
                            strategy[key] = record[key]
                    await cur.execute("""INSERT INTO ai_jobs
                        (service,task_type,task_key,operator_type,strategy,system_prompt,user_prompt,status,attempt_count,
                         prompt_tokens,completion_tokens,total_tokens,cached_tokens,round,created_at,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,'running',0,0,0,0,0,0,%s,%s)""",
                        (legacy.SITE_AI_SERVICE, record["task_type"], task_key, legacy.SITE_AI_OPERATOR_TYPE,
                         json.dumps(strategy, ensure_ascii=False), record.get("system_prompt"), record.get("user_prompt"),
                         timestamp(record.get("started_at")), timestamp(record.get("ended_at"))))
                    job_id = cur.lastrowid
                else:
                    job_id = prior[0]
                    strategy = json.loads(prior[1])
                if record["record_kind"] == "attempt":
                    # A settlement replacement removes the superseded attempt
                    # before the new one lands, so one model call never bills twice.
                    if record.get("supersedes_record_id"):
                        await cur.execute("DELETE FROM ai_job_attempts WHERE job_id=%s AND request_id=%s",
                                          (job_id, record["supersedes_record_id"]))
                    await cur.execute("SELECT id FROM ai_job_attempts WHERE job_id=%s AND request_id=%s LIMIT 1", (job_id, record_id))
                    if not await cur.fetchone():
                        prompt, completion, total, cached = _legacy_tokens(breakdown)
                        if cost is None:
                            pricing_source = "unmatched" if newapi_billing.enabled() else None
                            cost_quota, cost_usd, pricing_model = None, None, None
                        else:
                            pricing_source = cost["source"]
                            cost_quota, cost_usd, pricing_model = cost["quota"], cost["usd"], cost["pricing_model"]
                        await cur.execute("""INSERT INTO ai_job_attempts
                            (job_id,provider,model,attempt_no,round,is_fallback,started_at,ended_at,ok,prompt_tokens,
                             completion_tokens,total_tokens,cached_tokens,input_tokens,output_tokens,cache_read_tokens,
                             cache_write_tokens,reasoning_tokens,cost_quota,cost_usd,pricing_source,pricing_model,
                             error_kind,error_message,raw_response,request_id,selected)
                            VALUES (%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)""",
                            (job_id, record.get("provider") or "unknown", record.get("model"), record["attempt_no"],
                             timestamp(record.get("started_at")), timestamp(record.get("ended_at")), int(record.get("success", False)),
                             prompt, completion, total, cached,
                             breakdown["input_tokens"] if breakdown else None,
                             breakdown["output_tokens"] if breakdown else None,
                             breakdown["cache_read_tokens"] if breakdown else None,
                             breakdown["cache_write_tokens"] if breakdown else None,
                             breakdown["reasoning_tokens"] if breakdown else None,
                             cost_quota, cost_usd, pricing_source, pricing_model,
                             None if record.get("success") else "DSH_MODEL_FAILURE",
                             record.get("error_message") or None, record.get("response_content") or None, record_id))
                        await cur.execute("UPDATE ai_jobs SET last_provider=%s WHERE id=%s", (record.get("provider"), job_id))
                await cur.execute("""SELECT COUNT(*),COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(completion_tokens),0),
                    COALESCE(SUM(total_tokens),0),COALESCE(SUM(cached_tokens),0),
                    SUM(input_tokens),SUM(output_tokens),SUM(cache_read_tokens),SUM(cache_write_tokens),
                    SUM(reasoning_tokens),SUM(cost_quota),SUM(cost_usd),SUM(total_tokens IS NULL)
                    FROM ai_job_attempts WHERE job_id=%s""", (job_id,))
                count, prompt, completion, total, cached, input_tokens, output_tokens, cache_read, cache_write, reasoning, cost_quota, cost_usd, unknown = await cur.fetchone()
                strategy["usage_unknown_attempts"] = int(unknown or 0)
                await cur.execute("""UPDATE ai_jobs SET attempt_count=%s,prompt_tokens=%s,completion_tokens=%s,
                    total_tokens=%s,cached_tokens=%s,input_tokens=%s,output_tokens=%s,cache_read_tokens=%s,
                    cache_write_tokens=%s,reasoning_tokens=%s,cost_quota=%s,cost_usd=%s,strategy=%s,
                    updated_at=GREATEST(updated_at,%s) WHERE id=%s""",
                    (count, prompt, completion, total, cached, input_tokens, output_tokens, cache_read, cache_write,
                     reasoning, cost_quota, cost_usd, json.dumps(strategy, ensure_ascii=False), timestamp(record.get("ended_at")), job_id))
                if record.get("task_status"):
                    strategy["end_reason"] = record.get("end_reason")
                    await cur.execute("SELECT id,raw_response FROM ai_job_attempts WHERE job_id=%s AND ok=1 ORDER BY attempt_no DESC LIMIT 1", (job_id,))
                    final = await cur.fetchone()
                    await cur.execute("UPDATE ai_job_attempts SET selected=0 WHERE job_id=%s", (job_id,))
                    if final:
                        await cur.execute("UPDATE ai_job_attempts SET selected=1 WHERE id=%s", (final[0],))
                    await cur.execute("UPDATE ai_jobs SET status=%s,final_result=%s,last_error=%s,strategy=%s WHERE id=%s",
                        (record["task_status"], final[1] if final else None,
                         None if record["task_status"] == "success" else record.get("end_reason"), json.dumps(strategy, ensure_ascii=False), job_id))
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise
            finally:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (lock,))


def historical_record(record_id, payload):
    """Baseline requests without a compatibility link keep a stable isolated job.

    This is historical accounting, never a manufactured native turn or token bill.
    """
    return {**payload, "task_key": "dsh-legacy:" + record_id, "task_type": "dsh_legacy_attempt",
            "record_kind": "attempt", "attempt_no": 1,
            "task_status": "success" if payload.get("success") else "failed",
            "end_reason": "historical-attempt"}


async def drain_outbox():
    """Operator-only export before a consistent maintenance snapshot."""
    from .main import Infrastructure
    from .migrate import connect
    if not legacy.Config.SITE_AI_RECORDS_ENABLED:
        raise RuntimeError("website accounting must be enabled before retirement")
    app = Infrastructure.__new__(Infrastructure)
    app.conn = connect()
    exported = 0
    try:
        while True:
            with app.conn.cursor() as cur:
                cur.execute("SELECT record_id FROM yunying_ai_records WHERE mirror_status='pending' ORDER BY dsh_session_id,request_seq LIMIT 100")
                rows = list(cur.fetchall())
            if not rows:
                break
            for row in rows:
                result = await app.ai_record({"recordId": row["record_id"]})
                if not result["ok"]:
                    raise RuntimeError("AI accounting export deferred")
                with app.conn.cursor() as cur:
                    cur.execute("UPDATE yunying_ai_records SET mirror_status='mirrored' WHERE record_id=%s", (row["record_id"],))
                exported += 1
        return {"exported_records": exported}
    finally:
        app.conn.close()
        await legacy.close_call_log_pool()


if __name__ == "__main__":
    import argparse
    import asyncio
    parser = argparse.ArgumentParser(description="Drain the durable AI outbox without invoking a model")
    parser.add_argument("--drain", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(asyncio.run(drain_outbox())))
