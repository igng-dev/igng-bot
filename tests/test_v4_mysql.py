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


def test_real_mysql_migration_fifo_retry_restart_and_memory_qq_schema():
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
        assert journal.pending() is None  # recording precedes delivery
        assert journal.pending_recording()['event_id'] == first
        journal.prepare(first, {'eventId':first,'key':key,'kind':'message'})
        journal.prepare(second, {'eventId':second,'key':key,'kind':'message'})
        journal.prepare(third, {'eventId':third,'key':other_key,'kind':'message'})
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
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) total FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='memory_documents' AND COLUMN_NAME='person_qq'")
            assert cur.fetchone()['total'] == 1
    finally:
        if not conn.open:
            conn = connection()
        for identity in ids:
            Journal(conn).finish(identity)
        conn.close()


def test_real_mysql_unlinked_baseline_export_without_call_logs_is_idempotent(monkeypatch):
    conn = connection()
    # Website schema-shaped fixtures live only in the disposable test database.
    with conn.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS ai_jobs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,service VARCHAR(50),task_type VARCHAR(50),task_key VARCHAR(191),operator_type VARCHAR(20),operator_id BIGINT NULL,
          strategy JSON,system_prompt TEXT,user_prompt TEXT,status VARCHAR(20),attempt_count INT,prompt_tokens BIGINT,completion_tokens BIGINT,total_tokens BIGINT,cached_tokens BIGINT,
          input_tokens BIGINT,output_tokens BIGINT,cache_read_tokens BIGINT,cache_write_tokens BIGINT,reasoning_tokens BIGINT,cost_quota BIGINT,cost_usd DECIMAL(18,8),
          round INT,last_provider VARCHAR(100),last_error TEXT,final_result TEXT,created_at DATETIME,updated_at DATETIME) ENGINE=InnoDB''')
        cur.execute('''CREATE TABLE IF NOT EXISTS ai_job_attempts (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,job_id BIGINT,provider VARCHAR(50),model VARCHAR(80),attempt_no INT,round INT,is_fallback INT,
          started_at DATETIME,ended_at DATETIME,ok INT,prompt_tokens BIGINT,completion_tokens BIGINT,total_tokens BIGINT,cached_tokens BIGINT,
          input_tokens BIGINT,output_tokens BIGINT,cache_read_tokens BIGINT,cache_write_tokens BIGINT,reasoning_tokens BIGINT,cost_quota BIGINT,cost_usd DECIMAL(18,8),pricing_source VARCHAR(64),pricing_model VARCHAR(191),
          error_kind VARCHAR(50),error_message TEXT,raw_response TEXT,request_id VARCHAR(191),selected INT) ENGINE=InnoDB''')
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE ai_jobs MODIFY task_key VARCHAR(191)")
        for table,column,kind in [('ai_jobs','last_provider','VARCHAR(100)'),('ai_jobs','created_at','DATETIME'),('ai_jobs','updated_at','DATETIME'),
                                  ('ai_jobs','input_tokens','BIGINT'),('ai_jobs','output_tokens','BIGINT'),('ai_jobs','cache_read_tokens','BIGINT'),
                                  ('ai_jobs','cache_write_tokens','BIGINT'),('ai_jobs','reasoning_tokens','BIGINT'),('ai_jobs','cost_quota','BIGINT'),('ai_jobs','cost_usd','DECIMAL(18,8)'),
                                  ('ai_job_attempts','request_id','VARCHAR(191)'),('ai_job_attempts','error_kind','VARCHAR(50)'),
                                  ('ai_job_attempts','input_tokens','BIGINT'),('ai_job_attempts','output_tokens','BIGINT'),('ai_job_attempts','cache_read_tokens','BIGINT'),
                                  ('ai_job_attempts','cache_write_tokens','BIGINT'),('ai_job_attempts','reasoning_tokens','BIGINT'),('ai_job_attempts','cost_quota','BIGINT'),
                                  ('ai_job_attempts','cost_usd','DECIMAL(18,8)'),('ai_job_attempts','pricing_source','VARCHAR(64)'),('ai_job_attempts','pricing_model','VARCHAR(191)')]:
            cur.execute("SELECT COUNT(*) n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s", (table,column))
            if not cur.fetchone()['n']:cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
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
            a = await app.ai_record({'recordId':record_id})
            b = await app.ai_record({'recordId':record_id})
            assert a == b and a['ok']
            with conn.cursor() as cur:
                assert a['callLogId'] is None
                cur.execute('SELECT * FROM ai_jobs WHERE service=%s AND task_key=%s', ('igng-bot','dsh-legacy:'+record_id))
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


def test_real_mysql_group_pause_migration_can_resume_after_ddl_without_resetting_configuration():
    from pathlib import Path
    conn=connection()
    group=int(uuid4().int % 10**9)+10**9
    try:
        migrate(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO group_configs (group_id,is_chat_mode,social_paused) VALUES (%s,0,1)", (group,))
            source=Path(__file__).resolve().parents[1]/'migrations/v4/002_group_social_pause.sql'
            # Simulate DDL committed but checksum registration interrupted. Existing permission survives.
            for statement in source.read_text().split(';'):
                if statement.strip():cur.execute(statement)
            cur.execute('SELECT is_chat_mode,social_paused FROM group_configs WHERE group_id=%s',(group,))
            assert cur.fetchone()=={'is_chat_mode':0,'social_paused':1}
            cur.execute("SELECT COUNT(*) n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='group_configs' AND COLUMN_NAME='social_paused'")
            assert cur.fetchone()['n']==1
    finally:
        conn.close()


def test_recording_continues_while_dsh_delivery_retries_and_admin_toggle_is_once():
    from igngbot_v4.settings import Settings
    from igngbot_v3.db import DBHandler
    from unittest.mock import MagicMock
    class Response:
        async def __aenter__(self):raise ConnectionError()
        async def __aexit__(self,*_):return False
    conn=connection()
    group=int(uuid4().int%10**8)+5000000000
    config=SimpleNamespace(DB_HOST='127.0.0.1',DB_PORT=int(os.getenv('YUNYING_TEST_DB_PORT','33316')),DB_USER='root',DB_PASSWORD='',DB_NAME='yunying_v4_test')
    app=Infrastructure.__new__(Infrastructure)
    app.conn=conn;app.db=DBHandler(config, legacy_compat=False);app.journal=Journal(conn)
    app.settings=Settings(str(uuid4()),groups=frozenset({str(group)}));app._stopping=False
    app._journal_signal=asyncio.Event();app._delivery_signal=asyncio.Event()
    app.http=MagicMock();app.http.post.return_value=Response()
    ids=[]
    async def prepare(row):
        app.db.insert_message(group_id=group,sender_id=2001,reply_to_msg_id=None,msg_id=row['event_id'],message_content='附件已保存',plain_text_content='OCR/ASR记录',
            attachments_json=encode([{'type':'image','stored_path':'fixture.png','ocr_text':'图片文字'}]))
        return {'eventId':row['event_id'],'key':row['conversation_key'],'kind':'message'}
    app.prepare=prepare
    async def scenario():
        record=asyncio.create_task(app.record());deliver=asyncio.create_task(app.deliver())
        try:
            for mid in range(3):
                _,identity=app.journal.enqueue({'post_type':'message','group_id':group,'user_id':2001,'message_id':mid+1})
                ids.append(identity)
            for _ in range(200):
                with conn.cursor() as cur:
                    cur.execute('SELECT COUNT(*) n FROM message_logs WHERE group_id=%s',(group,))
                    if cur.fetchone()['n']==3:break
                await asyncio.sleep(.01)
            with conn.cursor() as cur:
                cur.execute('SELECT recording_status,delivery_status,prepared_event FROM yunying_ingress WHERE conversation_key=%s ORDER BY id',('group:'+str(group),))
                rows=cur.fetchall()
                assert len(rows)==3 and all(row['recording_status']=='recorded' and row['delivery_status']=='pending' for row in rows)
            # Retrying the same toggle after a crash cannot undo it.
            assert app.journal.group_control(ids[0],group,'is_chat_mode') is True
            assert app.journal.group_control(ids[0],group,'is_chat_mode') is True
            assert app.group_policy(group)=={'chatMode':True}
        finally:
            app._stopping=True;record.cancel();deliver.cancel()
            await asyncio.gather(record,deliver,return_exceptions=True)
    try:asyncio.run(scenario())
    finally:
        for identity in ids:app.journal.finish(identity)
        app.db.conn.close();conn.close()


def test_v4_message_initializer_does_not_create_or_seed_legacy_ai_context():
    from unittest.mock import MagicMock
    from igngbot_v3.db import DBHandler
    handler=DBHandler(SimpleNamespace(PROMPT_DIR='/tmp/no-v3-seed'))
    handler._conn=MagicMock()
    handler.init_message_tables()
    statements=' '.join(str(call.args[0]) for call in handler._conn.cursor.return_value.__enter__.return_value.execute.call_args_list)
    assert 'message_logs' in statements and 'message_recall_events' in statements
    assert 'context_summaries' not in statements and 'system_prompts' not in statements


def test_native_job_attempt_aggregation_unknown_usage_compaction_and_replay(monkeypatch):
    conn=connection()
    config=dict(host='127.0.0.1',port=int(os.getenv('YUNYING_TEST_DB_PORT','33316')),user='root',password='',db='yunying_v4_test',charset='utf8mb4')
    monkeypatch.setattr(call_log_db,'SITE_AI_DB_CONFIG',config)
    monkeypatch.setattr(call_log_db.Config,'SITE_AI_RECORDS_ENABLED',True)
    app=Infrastructure.__new__(Infrastructure);app.conn=conn
    sid=str(uuid4());key='dsh:'+sid+':turn:1'
    base={'task_key':key,'task_type':'social_turn','record_kind':'attempt','group_id':'1001','sender_id':'2001','sender_name':'fixture',
          'message_text':'自然沉默也要计费','call_type':'agent','model':'script','provider':'script','system_prompt':'official Session',
          'user_prompt':'自然沉默也要计费','response_content':'','tool_calls':[], 'duration_ms':20,'success':True,'error_message':'',
          'started_at':1700000000000,'ended_at':1700000000020,'native_turn':1,'native_step':1}
    records=[{**base,'attempt_no':1,'success':False,'token_usage':None},
             {**base,'attempt_no':2,'token_usage':{'inputTokens':20,'cacheReadTokens':10,'outputTokens':5}},
             {**base,'attempt_no':3,'token_usage':{'inputTokens':40,'cacheWriteTokens':2,'outputTokens':8}},
             {**base,'record_kind':'task-end','task_status':'success','end_reason':'completed'},
             {**base,'task_key':'dsh:'+sid+':compaction:fixture','task_type':'dsh_compaction','attempt_no':1,'token_usage':{'inputTokens':5,'outputTokens':2},'task_status':'success','end_reason':'completed'}]
    async def scenario():
        original_pool=call_log_db._get_site_pool
        failures=[True]
        async def recoverable_site():
            if failures and failures.pop():raise ConnectionError('synthetic website outage')
            return await original_pool()
        monkeypatch.setattr(call_log_db,'_get_site_pool',recoverable_site)
        try:
            for seq,record in enumerate(records,1):
                rid=f'{sid}:{seq}'
                with conn.cursor() as cur:cur.execute('INSERT INTO yunying_ai_records (record_id,dsh_session_id,request_seq,payload) VALUES (%s,%s,%s,%s)',(rid,sid,seq,encode(record)))
                if seq==1:
                    with pytest.raises(ConnectionError):await app.ai_record({'recordId':rid})
                    with conn.cursor() as cur:
                        cur.execute('SELECT mirror_status,call_log_id FROM yunying_ai_records WHERE record_id=%s',(rid,))
                        assert cur.fetchone()=={'mirror_status':'pending','call_log_id':None}
                assert (await app.ai_record({'recordId':rid}))['ok']
                assert (await app.ai_record({'recordId':rid}))['ok']
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM ai_jobs WHERE service=%s AND task_key=%s',('igng-bot',key));job=cur.fetchone()
                assert job['status']=='success' and job['attempt_count']==3
                assert (job['prompt_tokens'],job['completion_tokens'],job['total_tokens'],job['cached_tokens'])==(72,13,85,12)
                import json
                assert json.loads(job['strategy'])['usage_unknown_attempts']==1
                cur.execute('SELECT * FROM ai_job_attempts WHERE job_id=%s ORDER BY attempt_no',(job['id'],));attempts=cur.fetchall()
                assert len(attempts)==3 and attempts[0]['total_tokens'] is None
                assert [r['selected'] for r in attempts]==[0,0,1]
                assert len({r['request_id'] for r in attempts})==3
                cur.execute('SELECT COUNT(*) n FROM ai_jobs WHERE task_key LIKE %s',('dsh:'+sid+':%',));assert cur.fetchone()['n']==2
        finally:await call_log_db.close_call_log_pool()
    try:asyncio.run(scenario())
    finally:conn.close()


def test_native_settlement_replacement_deletes_the_superseded_attempt(monkeypatch):
    conn=connection()
    config=dict(host='127.0.0.1',port=int(os.getenv('YUNYING_TEST_DB_PORT','33316')),user='root',password='',db='yunying_v4_test',charset='utf8mb4')
    monkeypatch.setattr(call_log_db,'SITE_AI_DB_CONFIG',config)
    monkeypatch.setattr(call_log_db.Config,'SITE_AI_RECORDS_ENABLED',True)
    monkeypatch.setattr(call_log_db.Config,'NEWAPI_BASE_URL','')
    app=Infrastructure.__new__(Infrastructure);app.conn=conn
    sid=str(uuid4());key='dsh:'+sid+':turn:7'
    base={'task_key':key,'task_type':'social_turn','record_kind':'attempt','group_id':'1001','sender_id':'2001','sender_name':'fixture',
          'message_text':'替换结算','call_type':'agent','model':'cc/deepseek/deepseek-v4.1-flash','provider':'newapi','system_prompt':'official Session',
          'user_prompt':'替换结算','response_content':'','tool_calls':[],'duration_ms':20,'success':True,'error_message':'',
          'started_at':1700000000000,'ended_at':1700000000020,'native_turn':7,'native_step':1}
    def breakdown(input_tokens,output_tokens):
        # Same camelCase shape the live Node accounting writes.
        return {'input':input_tokens,'output':output_tokens,'cacheRead':0,'cacheWrite':0,
                'reasoning':0,'prompt':input_tokens,'completion':output_tokens,
                'total':input_tokens+output_tokens,'cached':0}
    first={**base,'attempt_no':1,'token_usage':{'inputTokens':10,'outputTokens':5},'token_breakdown':breakdown(10,5)}
    second={**base,'attempt_no':2,'token_usage':{'inputTokens':12,'outputTokens':6},'token_breakdown':breakdown(12,6),
            'supersedes_seq':1,'supersedes_record_id':f'{sid}:1'}
    async def scenario():
        try:
            for seq,record in [(1,first),(2,second)]:
                rid=f'{sid}:{seq}'
                with conn.cursor() as cur:cur.execute('INSERT INTO yunying_ai_records (record_id,dsh_session_id,request_seq,payload) VALUES (%s,%s,%s,%s)',(rid,sid,seq,encode(record)))
                assert (await app.ai_record({'recordId':rid}))['ok']
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM ai_jobs WHERE service=%s AND task_key=%s',('igng-bot',key));job=cur.fetchone()
                assert job['attempt_count']==1 and job['prompt_tokens']==12 and job['completion_tokens']==6
                assert job['input_tokens']==12 and job['output_tokens']==6
                cur.execute('SELECT request_id FROM ai_job_attempts WHERE job_id=%s',(job['id'],));rows=cur.fetchall()
                assert [r['request_id'] for r in rows]==[f'{sid}:2']
        finally:await call_log_db.close_call_log_pool()
    try:asyncio.run(scenario())
    finally:conn.close()
