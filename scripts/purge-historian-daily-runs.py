#!/usr/bin/env python3
"""一次性清理：删除 AI 时代的日报 run（日报已改为纯机械统计）。

日报不再调用模型，历史上由 DSH 生成的日报正文、证据快照与调用映射按用户决定整行删除。
保留 AI 计费账本（`yunying_ai_records` / `ai_jobs` / `ai_job_attempts`），只删除报告域数据：
`historian_runs` 及其 `historian_events` / `historian_receipts` / `historian_calls`，
以及删空后的 `historian_reports`（日报实体，重建由 Bot 调度负责）。
新机械日报（`phase=mechanical_publish`）不在清理范围内，脚本可安全地在新版本上线后执行。

默认 dry-run 只统计；确认清单后用 `--apply` 执行。任一目标 run 处于 pending/running
或仍存在未导出的 AI 账本时脚本会拒绝执行，避免打断进行中的任务或丢失计费。

用法（在 Bot 环境变量下运行，或进入 Bot 容器执行）：
  python scripts/purge-historian-daily-runs.py                 # 干跑，列出将删除的数量
  python scripts/purge-historian-daily-runs.py --apply         # 执行
  python scripts/purge-historian-daily-runs.py --server 1 --apply
  python scripts/purge-historian-daily-runs.py --before '2026-10-14 00:00:00' --apply
"""

import argparse
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from igngbot_v4.migrate import connect  # noqa: E402

CHUNK = 500


def chunks(values):
    for index in range(0, len(values), CHUNK):
        yield values[index:index + CHUNK]


def placeholders(values):
    return ','.join(['%s'] * len(values))


def count(cur, table, runs):
    total = 0
    for part in chunks(runs):
        cur.execute(f'SELECT COUNT(*) n FROM {table} WHERE run_id IN ({placeholders(part)})', part)
        total += int(cur.fetchone()['n'])
    return total


def main():
    parser = argparse.ArgumentParser(description='删除 AI 时代的日报 run（保留计费账本）')
    parser.add_argument('--apply', action='store_true', help='执行删除；缺省仅统计')
    parser.add_argument('--before', default=None, help='仅处理该 UTC 时间之前创建的 run，ISO 8601，缺省为当前时间')
    parser.add_argument('--server', type=int, default=None, help='仅处理指定 server_id')
    args = parser.parse_args()
    cutoff = args.before or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    with connect() as conn:
        with conn.cursor() as cur:
            clauses = ["p.kind='daily'", "r.phase<>'mechanical_publish'", 'r.created_at<%s']
            values = [cutoff]
            if args.server is not None:
                clauses.append('p.server_id=%s')
                values.append(args.server)
            cur.execute('SELECT r.id,r.status,r.report_id,p.server_id,p.period_start FROM historian_runs r '
                        'JOIN historian_reports p ON p.id=r.report_id WHERE ' + ' AND '.join(clauses) + ' ORDER BY p.period_start', values)
            runs = cur.fetchall()
            if not runs:
                print(f'没有符合条件的日报 run（cutoff={cutoff}）。无需清理。')
                return
            run_ids = [row['id'] for row in runs]
            report_ids = sorted({row['report_id'] for row in runs})
            active = [row for row in runs if row['status'] in {'pending', 'running'}]
            if active:
                print(f'拒绝执行：{len(active)} 个目标 run 仍在 pending/running，先等待其结束（或停调度后重试）。')
                for row in active[:10]:
                    print(f"  {row['id']} {row['status']} 服务器 {row['server_id']} {row['period_start']}")
                raise SystemExit(1)
            pending_exports = 0
            for part in chunks(run_ids):
                cur.execute('SELECT COUNT(*) n FROM yunying_ai_records a JOIN historian_calls h ON h.record_id=a.record_id '
                            f'WHERE a.mirror_status=%s AND h.run_id IN ({placeholders(part)})', ('pending', *part))
                pending_exports += int(cur.fetchone()['n'])
            if pending_exports:
                print(f'拒绝执行：{pending_exports} 条 AI 账本尚未导出到 ai_jobs，先让 Bot 镜像循环完成后再试。')
                raise SystemExit(1)
            events = count(cur, 'historian_events', run_ids)
            receipts = count(cur, 'historian_receipts', run_ids)
            calls = count(cur, 'historian_calls', run_ids)
            periods = sorted({str(row['period_start']) for row in runs})
            print(f'目标：日报 run {len(run_ids)} 个（报告实体 {len(report_ids)} 个，周期 {periods[0]} .. {periods[-1]}，cutoff={cutoff}）')
            print(f'将删除：historian_events {events} 行、historian_receipts {receipts} 行、historian_calls {calls} 行、'
                  f'historian_runs {len(run_ids)} 行，以及删空后的 historian_reports')
            print('保留：yunying_ai_records / ai_jobs / ai_job_attempts（计费与调用账本）')
            if not args.apply:
                print('当前为 dry-run；确认后加 --apply 执行。')
                return
            conn.begin()
            try:
                for part in chunks(run_ids):
                    cur.execute(f'UPDATE historian_reports SET current_run_id=NULL WHERE current_run_id IN ({placeholders(part)})', part)
                    cur.execute(f'DELETE FROM historian_events WHERE run_id IN ({placeholders(part)})', part)
                    cur.execute(f'DELETE FROM historian_receipts WHERE run_id IN ({placeholders(part)})', part)
                    cur.execute(f'DELETE FROM historian_calls WHERE run_id IN ({placeholders(part)})', part)
                    cur.execute(f'DELETE FROM historian_runs WHERE id IN ({placeholders(part)})', part)
                for part in chunks(report_ids):
                    cur.execute(f'DELETE FROM historian_reports WHERE id IN ({placeholders(part)}) '
                                'AND NOT EXISTS (SELECT 1 FROM historian_runs WHERE report_id=historian_reports.id)', part)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            print('已删除。新日报会由 Bot 机械发布流程按周期重建。')


if __name__ == '__main__':
    main()
