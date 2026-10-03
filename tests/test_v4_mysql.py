"""Opt-in integration on a disposable loopback database, never deployment credentials."""
import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pymysql
from igngbot_v4.journal import Journal, ingress_identity, encode
from igngbot_v4.migrate import migrate
from igngbot_v4.main import Infrastructure
from igngbot_v3 import call_log_db

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test', reason='disposable local MySQL opt-in')


def connection():
    return pymysql.connect(host='127.0.0.1', port=int(os.getenv('YUNYING_TEST_DB_PORT', '33316')), user='root', database='yunying_v4_test', autocommit=True, charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor, init_command="SET time_zone = '+00:00'")


def test_real_mysql_migration_fifo_retry_restart_and_identity_consent():
    conn = connection()
    journal = Journal(conn)
    mid = int(uuid4().int % 10**12)
    group = int(uuid4().int % 10**9) + 10**9
    raw = {'post_type':'message','group_id':group,'user_id':2001,'message_id':mid,'message':'第一条'}
    ids = []
    try:
        migrate(conn)
        migrate(conn)
        key, first = journal.enqueue(raw)
        ids.append(first)
        assert journal.enqueue(raw)[1] == first
        _, second = journal.enqueue({**raw,'message_id':mid+1,'message':'第二条'})
        ids.append(second)
        other_key, third = journal.enqueue({**raw,'group_id':group+1,'message_id':mid+2})
        ids.append(third)
        assert journal.pending()['event_id'] == first
        journal.prepare(first, {'eventId':first,'key':key,'kind':'message'})
        journal.retry(first, 0, RuntimeError())
        assert journal.pending()['event_id'] == third  # another conversation can progress past a backed-off head
        journal.finish(third)
        assert journal.pending() is None             # second cannot overtake first
        conn.close()
        conn = connection()
        journal = Journal(conn)
        with conn.cursor() as cur:
            cur.execute('SELECT prepared_event FROM yunying_ingress WHERE event_id=%s', (first,))
            assert first in cur.fetchone()['prepared_event']
            cur.execute('UPDATE yunying_ingress SET available_at=UTC_TIMESTAMP(6) WHERE event_id=%s', (first,))
        assert journal.pending()['event_id'] == first
        journal.finish(first)
        assert journal.pending()['event_id'] == second
        journal.finish(second)
        qq = str(int(uuid4().int % 10**9)+10**9)
        journal.set_sharing(qq, True)
        journal.set_sharing(qq, False)
        with conn.cursor() as cur:
            cur.execute("SELECT identity_id,shared_memory_opt_in FROM memory_identity_bindings WHERE provider='qq' AND external_id=%s", (qq,))
            assert cur.fetchone()['shared_memory_opt_in'] == 0
            cur.execute("SELECT COUNT(*) total FROM memory_identity_audit WHERE external_id=%s AND operation='sharing-consent'", (qq,))
            assert cur.fetchone()['total'] == 2
    finally:
        if not conn.open:
            conn = connection()
        for identity in ids:
            Journal(conn).finish(identity)
        conn.close()


def test_real_mysql_call_log_and_site_mirror_retry_are_idempotent(monkeypatch):
    conn = connection()
    # Website schema-shaped fixtures live only in the disposable test database.
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS ai_jobs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,service VARCHAR(50),task_type VARCHAR(50),task_key VARCHAR(80),operator_type VARCHAR(20),operator_id BIGINT NULL,
          strategy JSON,system_prompt TEXT,user_prompt TEXT,status VARCHAR(20),attempt_count INT,prompt_tokens BIGINT,completion_tokens BIGINT,total_tokens BIGINT,cached_tokens BIGINT,
          round INT,last_error TEXT,final_result TEXT) ENGINE=InnoDB''')
        cur.execute('''CREATE TABLE IF NOT EXISTS ai_job_attempts (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,job_id BIGINT,provider VARCHAR(50),model VARCHAR(80),attempt_no INT,round INT,is_fallback INT,
          started_at DATETIME,ended_at DATETIME,ok INT,prompt_tokens BIGINT,completion_tokens BIGINT,total_tokens BIGINT,cached_tokens BIGINT,error_message TEXT,raw_response TEXT,selected INT) ENGINE=InnoDB''')
    config = dict(host='127.0.0.1',port=int(os.getenv('YUNYING_TEST_DB_PORT','33316')),user='root',password='',db='yunying_v4_test',charset='utf8mb4')
    monkeypatch.setattr(call_log_db, 'DB_CONFIG', config)
    monkeypatch.setattr(call_log_db, 'SITE_AI_DB_CONFIG', config)
    monkeypatch.setattr(call_log_db.Config, 'SITE_AI_RECORDS_ENABLED', True)
    record_id = str(uuid4())
    payload = {'group_id':'1001','sender_id':'2001','sender_name':'fixture','message_text':'晴天','call_type':'agent','model':'script','provider':'script',
               'system_prompt':'official Session','user_prompt':'天气','response_content':'晴天','tool_calls':[],
               'token_usage':{'inputTokens':20,'cacheReadTokens':10,'outputTokens':5},'duration_ms':100,'success':True,'error_message':''}
    with conn.cursor() as cur:
        cur.execute('INSERT INTO yunying_ai_records (record_id,dsh_session_id,request_seq,payload) VALUES (%s,%s,1,%s)', (record_id,str(uuid4()),encode(payload)))
    app = Infrastructure.__new__(Infrastructure)
    app.conn = conn
    async def run():
        try:
            await call_log_db.ensure_call_logs_table()
            a = await app.ai_record({'recordId':record_id})
            b = await app.ai_record({'recordId':record_id})
            assert a == b and a['ok']
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) total FROM call_logs WHERE id=%s', (a['callLogId'],))
                assert cur.fetchone()['total'] == 1
                cur.execute('SELECT * FROM ai_jobs WHERE service=%s AND task_key=%s', ('igng-bot',str(a['callLogId'])))
                rows = cur.fetchall()
                assert len(rows) == 1
                assert rows[0]['prompt_tokens'] == 30
                cur.execute('SELECT provider,COUNT(*) total FROM ai_job_attempts WHERE job_id=%s GROUP BY provider', (rows[0]['id'],))
                assert cur.fetchone() == {'provider':'script','total':1}
        finally:
            await call_log_db.close_call_log_pool()
    try:
        asyncio.run(run())
    finally:
        conn.close()
