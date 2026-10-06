#!/usr/bin/env python3
"""对账：tokscale 计数、bot 记账与 new-api 真实账单。

三个独立来源：
1. tokscale 直接解析 DSH session（计数权威，`--client dsh`）；
2. `igng_sites.ai_jobs/ai_job_attempts`（bot 按 turn/attempt 落库的记录）；
3. new-api 日志（真实账单，quota 与倍率）。

默认只读、只报告；`--fail-on-drift` 时漂移超阈值返回非零，供定时任务告警。
tokscale 需在 PATH（或 TOKSCALE_BIN）中；new-api 需要 NEWAPI_* 配置。

用法：
  NEWAPI_BASE_URL=... NEWAPI_API_KEY=... DB_HOST=... SITE_AI_DB_HOST=... \\
    python scripts/accounting-reconcile.py --since 2026-10-01 --fail-on-drift
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from igngbot_v3.call_log_db import SITE_AI_DB_CONFIG  # noqa: E402
from igngbot_v4 import newapi_billing  # noqa: E402

TOKEN_BUCKETS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def tokscale_sessions(home: str | None) -> dict:
    binary = os.getenv("TOKSCALE_BIN") or shutil.which("tokscale")
    if not binary:
        raise RuntimeError("tokscale 不在 PATH 中；安装或设置 TOKSCALE_BIN")
    env = dict(os.environ)
    if home:
        env["DSH_HOME"] = home
    result = subprocess.run(
        [binary, "--client", "dsh", "--json", "--no-spinner", "--group-by", "session,model"],
        capture_output=True, text=True, env=env, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"tokscale 退出码 {result.returncode}: {result.stderr.strip()[:300]}")
    payload = json.loads(result.stdout)
    sessions = {}
    for entry in payload.get("entries", []):
        session = entry.get("sessionId") or "unknown"
        row = sessions.setdefault(session, {k: 0 for k in TOKEN_BUCKETS})
        row["input_tokens"] += int(entry.get("input") or 0)
        row["output_tokens"] += int(entry.get("output") or 0)
        row["cache_read_tokens"] += int(entry.get("cacheRead") or 0)
        row["cache_write_tokens"] += int(entry.get("cacheWrite") or 0)
        row["reasoning_tokens"] += int(entry.get("reasoning") or 0)
    return sessions


def bot_sessions(since: str | None) -> dict:
    import pymysql
    config = dict(SITE_AI_DB_CONFIG)
    connection = pymysql.connect(host=config["host"], port=int(config["port"]), user=config["user"],
                                 password=config["password"], database=config["db"], charset="utf8mb4",
                                 cursorclass=pymysql.cursors.DictCursor)
    try:
        with connection.cursor() as cur:
            where = "WHERE j.service='igng-bot'"
            params = []
            if since:
                where += " AND j.created_at>=%s"
                params.append(since)
            cur.execute(f"""SELECT JSON_UNQUOTE(JSON_EXTRACT(IF(JSON_VALID(j.strategy),j.strategy,'{{}}'),'$.dsh_session_id')) session_id,
                COALESCE(SUM(a.input_tokens),0) input_tokens,COALESCE(SUM(a.output_tokens),0) output_tokens,
                COALESCE(SUM(a.cache_read_tokens),0) cache_read_tokens,COALESCE(SUM(a.cache_write_tokens),0) cache_write_tokens,
                COALESCE(SUM(a.reasoning_tokens),0) reasoning_tokens,COALESCE(SUM(a.cost_quota),0) cost_quota,
                COALESCE(SUM(a.cost_usd),0) cost_usd
                FROM ai_jobs j JOIN ai_job_attempts a ON a.job_id=j.id {where}
                GROUP BY session_id""", params)
            return {row["session_id"] or "unknown": {k: int(row[k] or 0) for k in (*TOKEN_BUCKETS, "cost_quota")}
                    | {"cost_usd": float(row["cost_usd"] or 0)} for row in cur.fetchall()}
    finally:
        connection.close()


def total(row: dict) -> int:
    return sum(int(row.get(bucket) or 0) for bucket in TOKEN_BUCKETS)


def compare(counted: dict, recorded: dict) -> dict:
    report = {"sessions": {}, "totals": {"counted": 0, "recorded": 0, "delta": 0}}
    for session in sorted(set(counted) | set(recorded)):
        counted_total = total(counted.get(session, {}))
        recorded_total = total(recorded.get(session, {}))
        report["sessions"][session] = {"counted": counted_total, "recorded": recorded_total,
                                       "delta": recorded_total - counted_total}
        report["totals"]["counted"] += counted_total
        report["totals"]["recorded"] += recorded_total
    report["totals"]["delta"] = report["totals"]["recorded"] - report["totals"]["counted"]
    return report


async def newapi_report() -> dict:
    if not newapi_billing.enabled():
        return {"enabled": False, "logs": 0, "quota": 0, "models": {}}
    logs = await newapi_billing.refresh_logs(force=True)
    models = {}
    quota = 0
    for item in logs.get("items", []):
        models[item["model"]] = models.get(item["model"], 0) + item["quota"]
        quota += item["quota"]
    return {"enabled": True, "logs": len(logs.get("items", [])), "quota": quota, "models": models,
            "error": logs.get("error", "")}


def main() -> int:
    parser = argparse.ArgumentParser(description="tokscale / bot 记账 / new-api 三方对账")
    parser.add_argument("--home", help="DSH_HOME（默认取环境变量或 ~/.dsh）")
    parser.add_argument("--since", help="只统计该日期之后的 bot 记录（YYYY-MM-DD）")
    parser.add_argument("--tolerance", type=float, default=0.01, help="允许的相对漂移，默认 1%%")
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args()
    import asyncio
    recorded = bot_sessions(args.since)
    report = {"token": compare(tokscale_sessions(args.home), recorded),
              "newapi": asyncio.run(newapi_report())}
    recorded_cost = sum(row.get("cost_quota", 0) for row in recorded.values())
    report["cost"] = {"recorded_quota": recorded_cost, "newapi_quota": report["newapi"]["quota"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    totals = report["token"]["totals"]
    drift = abs(totals["delta"]) / max(1, totals["counted"])
    return 1 if args.fail_on_drift and drift > args.tolerance else 0


if __name__ == "__main__":
    raise SystemExit(main())
