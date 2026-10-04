"""Native DSH task/attempt export to the existing website AI tables.

The bot outbox and official Session are durable inputs. This adapter owns only SQL
accounting, never model calls. V3's legacy mirroring remains a rollback adapter.
"""
import hashlib
import json
from datetime import datetime, timezone
from igngbot_v3 import call_log_db as legacy


def tokens(usage):
    # Missing/partial provider accounting is unknown, never a fabricated zero bill.
    if not isinstance(usage, dict) or not any(k in usage for k in ("inputTokens", "prompt_tokens", "input_tokens")) or not any(k in usage for k in ("outputTokens", "completion_tokens", "output_tokens")):
        return None, None, None, None
    return legacy._extract_tokens(usage)


def timestamp(value):
    return datetime.fromtimestamp(float(value) / 1000, timezone.utc).replace(tzinfo=None) if value is not None else datetime.now(timezone.utc).replace(tzinfo=None)


async def mirror_native_record(record_id, record):
    if not legacy.Config.SITE_AI_RECORDS_ENABLED:
        return True
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
                                "accounting_version": 2, "usage_source": "provider"}
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
                    await cur.execute("SELECT id FROM ai_job_attempts WHERE job_id=%s AND request_id=%s LIMIT 1", (job_id, record_id))
                    if not await cur.fetchone():
                        prompt, completion, total, cached = tokens(record.get("token_usage"))
                        await cur.execute("""INSERT INTO ai_job_attempts
                            (job_id,provider,model,attempt_no,round,is_fallback,started_at,ended_at,ok,prompt_tokens,
                             completion_tokens,total_tokens,cached_tokens,error_kind,error_message,raw_response,request_id,selected)
                            VALUES (%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)""",
                            (job_id,record.get("provider") or "unknown",record.get("model"),record["attempt_no"],
                             timestamp(record.get("started_at")),timestamp(record.get("ended_at")),int(record.get("success",False)),
                             prompt,completion,total,cached,None if record.get("success") else "DSH_MODEL_FAILURE",
                             record.get("error_message") or None,record.get("response_content") or None,record_id))
                        await cur.execute("UPDATE ai_jobs SET last_provider=%s WHERE id=%s", (record.get("provider"),job_id))
                await cur.execute("""SELECT COUNT(*),COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(completion_tokens),0),
                    COALESCE(SUM(total_tokens),0),COALESCE(SUM(cached_tokens),0),SUM(total_tokens IS NULL)
                    FROM ai_job_attempts WHERE job_id=%s""", (job_id,))
                count,prompt,completion,total,cached,unknown = await cur.fetchone()
                strategy["usage_unknown_attempts"] = int(unknown or 0)
                await cur.execute("""UPDATE ai_jobs SET attempt_count=%s,prompt_tokens=%s,completion_tokens=%s,
                    total_tokens=%s,cached_tokens=%s,strategy=%s,updated_at=GREATEST(updated_at,%s) WHERE id=%s""",
                    (count,prompt,completion,total,cached,json.dumps(strategy,ensure_ascii=False),timestamp(record.get("ended_at")),job_id))
                if record.get("task_status"):
                    strategy["end_reason"] = record.get("end_reason")
                    await cur.execute("SELECT id,raw_response FROM ai_job_attempts WHERE job_id=%s AND ok=1 ORDER BY attempt_no DESC LIMIT 1", (job_id,))
                    final = await cur.fetchone()
                    await cur.execute("UPDATE ai_job_attempts SET selected=0 WHERE job_id=%s", (job_id,))
                    if final:
                        await cur.execute("UPDATE ai_job_attempts SET selected=1 WHERE id=%s", (final[0],))
                    await cur.execute("UPDATE ai_jobs SET status=%s,final_result=%s,last_error=%s,strategy=%s WHERE id=%s",
                        (record["task_status"],final[1] if final else None,
                         None if record["task_status"]=="success" else record.get("end_reason"),json.dumps(strategy,ensure_ascii=False),job_id))
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
