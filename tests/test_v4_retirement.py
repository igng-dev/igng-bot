"""Reversible retirement on synthetic, isolated loopback schemas only."""
import gzip
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pymysql
import pytest

from igngbot_v3.db import DBHandler
from igngbot_v4.journal import Journal, encode
from igngbot_v4.migrate import migrate
from igngbot_v4.retire import Retirement, LEGACY_TABLES, digest, exists, table_snapshot, server_identity, prove_restore

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test', reason='disposable local MySQL opt-in')


@pytest.fixture
def retired_db(tmp_path):
    name = 'yunying_v4_test_retirement_' + uuid4().hex[:8]
    opts = dict(host='127.0.0.1', port=int(os.getenv('YUNYING_TEST_DB_PORT', '33316')), user='root', password='', charset='utf8mb4', autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    admin = pymysql.connect(**opts)
    with admin.cursor() as cur:
        cur.execute('CREATE DATABASE `' + name + '` CHARACTER SET utf8mb4')
    conn = pymysql.connect(**opts, database=name)
    config = SimpleNamespace(DB_HOST='127.0.0.1', DB_PORT=opts['port'], DB_USER='root', DB_PASSWORD='', DB_NAME=name, PROMPT_DIR='/tmp/no-v3-seed')
    db = DBHandler(config)
    db.init_table(); db.init_group_configs_table(); migrate(conn)
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE personality_profiles (id INT PRIMARY KEY,name VARCHAR(100),prompt_text TEXT)')
        cur.execute('CREATE TABLE group_personality_configs (group_id BIGINT PRIMARY KEY,personality_id INT)')
        cur.execute("INSERT INTO personality_profiles VALUES (1,'fixture','原 Prompt，不修改')")
        cur.execute('INSERT INTO group_personality_configs VALUES (1001,1)')
        cur.execute("INSERT INTO context_summaries (group_id,summary_text,summarized_through_id) VALUES (1001,'旧摘要',1)")
        cur.execute("INSERT INTO system_prompts (prompt_key,prompt_text) VALUES ('chat','完整旧文本')")
        cur.execute('CREATE TABLE call_logs (id BIGINT PRIMARY KEY,group_id VARCHAR(50),sender_id VARCHAR(50),task_id BIGINT NULL,sender_name VARCHAR(100),message_text TEXT,call_type VARCHAR(32),model VARCHAR(100),system_prompt TEXT,user_prompt TEXT,thinking_content TEXT,response_content TEXT,tool_calls JSON,token_usage JSON,duration_ms INT,success TINYINT,error_message TEXT,created_at DATETIME)')
        for number in (1, 2, 3):
            cur.execute('INSERT INTO call_logs VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (number,'1001','2001',8 if number==1 else None,'fixture','问题','agent','script','相同系统 Prompt','原始提问'+str(number),None if number==1 else '', '原始响应',encode([]),None if number==1 else encode({'total_tokens':12}),25,1,'',datetime(2026,1,1)))
        cur.execute("INSERT INTO group_configs (group_id,is_chat_mode) VALUES (1001,0)")
    db.insert_message(group_id=1001,sender_id=2001,message_content='语音',reply_to_msg_id=None,msg_id='fixture-audio',
        attachments_json=encode([{'type':'audio','stored_path':'fixture.wav','transcript':'转写','text_extraction_status':'ok'}]),
        file_url='fixture.wav',file_type='audio',audio_file_path='fixture.wav',audio_transcript='转写')
    db.insert_message(group_id=1001,sender_id=2001,message_content='图片',reply_to_msg_id=None,msg_id='fixture-image',
        attachments_json=encode([{'type':'image','stored_path':'fixture.png','ocr_text':'OCR'}]),file_url='fixture.png',file_type='image')
    journal = Journal(conn)
    key, event = journal.enqueue({'group_id':1001,'user_id':2001,'message_id':7001,'post_type':'message'})
    journal.prepare(event, {'eventId':event,'key':key,'kind':'message'}); journal.finish(event)
    with conn.cursor() as cur:
        cur.execute('UPDATE yunying_ingress SET delivered_at=%s WHERE event_id=%s', (datetime.now()-timedelta(days=31), event))
        cur.execute("INSERT INTO yunying_sessions (conversation_key,conversation_type,external_id,dsh_session_id,provisioning_status) VALUES ('group:1001','group','1001',%s,'ready')", (str(uuid4()),))
    archive = tmp_path/'fixture.sql.gz'; archive.write_bytes(gzip.compress(b'-- synthetic proof fixture\n'))
    import hashlib
    with conn.cursor() as cur:
        cur.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')
        names = [r['TABLE_NAME'] for r in cur.fetchall()]
    snapshots = {n:table_snapshot(conn,n) for n in names}
    proof = {'format':1,'restore_verified':True,'database':name,'server_uuid':server_identity(conn),
        'backup_file':archive.name,'backup_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'tables':{n:{k:s[k] for k in ('row_count','content_sha256','schema_sha256')} for n,s in snapshots.items()}}
    manifest = tmp_path/'restore-proof.json'; manifest.write_text(json.dumps(proof))
    try:
        yield conn, db, config, Retirement(conn, manifest), snapshots, event, manifest
    finally:
        db.close(); conn.close()
        # Only this freshly generated fixture schema on the fixed loopback server.
        with admin.cursor() as cur: cur.execute('DROP DATABASE `' + name + '`')
        admin.close()


def test_retire_restore_preserves_prompts_tokens_media_tombstones_and_session(retired_db):
    conn, legacy, config, retire, snapshots, event, manifest = retired_db
    session = table_snapshot(conn,'yunying_sessions')['content_sha256']
    assert retire.legacy_tables(apply=True)['archived_rows'] == 4
    assert all(not exists(conn,n) for n in LEGACY_TABLES)
    assert retire.call_history(apply=True)['archived_calls'] == 3
    assert not exists(conn,'call_logs')
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) n FROM yunying_prompt_blobs'); assert cur.fetchone()['n'] == 5
    assert retire.media_columns(apply=True)['archived_media_rows'] == 2
    legacy.close()
    v4 = DBHandler(config, legacy_compat=False)
    try:
        v4.init_message_tables()  # Restart never recreates retired columns/tables.
        assert v4.mark_message_recalled(1001,'fixture-image')['status'] == 'marked'
        assert v4.get_recent_messages(1001,10)[-1]['is_recalled'] == 1
        assert v4.get_message_by_msg_id(1001,'fixture-audio')['audio_transcript'] == '转写'
        assert len(v4.get_messages_before(1001,None,10)) == 2
        assert len(v4.get_messages_from_msg_id(1001,'fixture-audio',10)) == 2
        assert len(v4.get_messages_after_id(1001,0,10)) == 2
        v4.mark_message_recalled(1001,'future')
        v4.insert_message(group_id=1001,sender_id=2001,message_content='迟到',reply_to_msg_id=None,msg_id='future')
        assert v4.get_message_by_msg_id(1001,'future')['is_recalled'] == 1
    finally: v4.close()
    # Ingress is unchanged by the mechanical message edits above and still has its verified proof.
    assert retire.ingress(apply=True)['archived_ingress'] == 1
    assert Journal(conn).enqueue({'group_id':1001,'user_id':2001,'message_id':7001,'post_type':'message'})[1] == event
    assert table_snapshot(conn,'yunying_sessions')['content_sha256'] == session
    retire.restore(apply=True)
    for name in (*LEGACY_TABLES,'call_logs'):
        assert table_snapshot(conn,name)['content_sha256'] == snapshots[name]['content_sha256']
    assert table_snapshot(conn,'yunying_sessions')['content_sha256'] == session
    with conn.cursor() as cur:
        cur.execute('SELECT raw_event,prepared_event,payload_archived_at FROM yunying_ingress WHERE event_id=%s',(event,))
        row = cur.fetchone(); assert row['payload_archived_at'] is None
        assert json.loads(row['raw_event'])['message_id'] == 7001
        cur.execute("SELECT is_recalled,file_url FROM message_logs WHERE msg_id='fixture-image'")
        assert cur.fetchone() == {'is_recalled':1,'file_url':'fixture.png'}
    with conn.cursor() as cur:
        cur.execute("UPDATE message_logs SET file_url='operator-new.png',audio_file_path=NULL WHERE msg_id='fixture-image'")
    retire.restore(apply=True)  # A repeated restore never replaces V3/operator changes.
    with conn.cursor() as cur:
        cur.execute("SELECT file_url,audio_file_path FROM message_logs WHERE msg_id='fixture-image'")
        assert cur.fetchone() == {'file_url':'operator-new.png','audio_file_path':None}


def test_retirement_refuses_missing_stale_proof_running_owner_and_unresolved_send(retired_db):
    conn, db, cfg, retire, snaps, event, manifest = retired_db
    with pytest.raises(ValueError, match='requires --proof'): Retirement(conn).legacy_tables(apply=True)
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(CONCAT('yunying-dsh:',DATABASE()),0)")
    with pytest.raises(RuntimeError, match='stop bot'): retire.legacy_tables(apply=True)
    with conn.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(CONCAT('yunying-dsh:',DATABASE()))")
        cur.execute("INSERT INTO yunying_sends (request_id,conversation_key,payload_hash,payload,status) VALUES ('fixture-unknown','group:1001',%s,'{}','unknown')", ('0'*64,))
    with pytest.raises(RuntimeError, match='unknown'): retire.legacy_tables(apply=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE yunying_sends SET status='failed' WHERE request_id='fixture-unknown'")
        cur.execute("UPDATE system_prompts SET prompt_text='归档后新改动'")
    with pytest.raises(ValueError, match='stale'): retire.legacy_tables(apply=True)
    assert all(exists(conn,n) for n in LEGACY_TABLES)
    with pytest.raises(ValueError, match='different MySQL server'): prove_restore(conn,conn,manifest,manifest.parent/'bad-proof.json')


def test_retirement_rejects_uncovered_attachment_and_corrupt_prompt(retired_db):
    conn, db, cfg, retire, snaps, event, manifest = retired_db
    with conn.cursor() as cur:
        cur.execute("UPDATE message_logs SET file_url='uncovered.png' WHERE msg_id='fixture-image'")
    with pytest.raises(ValueError, match='uncovered'): retire.media_columns(apply=True)
    retire.call_history(apply=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE yunying_prompt_blobs SET content='篡改' WHERE content=%s", ('相同系统 Prompt',))
    with pytest.raises(ValueError, match='corrupt'): retire.restore(apply=True)
    assert not exists(conn,'call_logs')


def test_ingress_retention_excludes_pending_and_new_rows_and_keeps_control_idempotency(retired_db):
    conn, db, cfg, retire, snaps, event, manifest = retired_db
    assert retire.ingress()['eligible'] == 1
    with pytest.raises(ValueError, match='minimum'): retire.ingress(days=29)
    journal = Journal(conn)
    _, control = journal.enqueue({'post_type':'message','group_id':1001,'user_id':2001,'message_id':8001})
    assert journal.group_control(control,1001,'is_chat_mode',True)
    journal.prepare(control, None); journal.finish(control)
    # Changing the queue after its restore proof prevents applying a stale snapshot.
    with pytest.raises(ValueError, match='stale'): retire.ingress(apply=True)
    assert journal.group_control(control,1001,'is_chat_mode',False)  # Prior command result wins.


def test_ddl_interruption_resumes_without_replacing_archived_data(retired_db, monkeypatch):
    conn, db, cfg, retire, snaps, event, manifest = retired_db
    original = pymysql.cursors.DictCursor.execute
    fault = [True]
    def fail_once(cursor, sql, args=None):
        if sql.startswith('DROP TABLE') and fault and fault.pop():
            raise RuntimeError('synthetic interrupted DDL')
        return original(cursor, sql, args)
    with monkeypatch.context() as scoped:
        scoped.setattr(pymysql.cursors.DictCursor,'execute',fail_once)
        with pytest.raises(RuntimeError,match='interrupted'): retire.legacy_tables(apply=True)
    assert all(exists(conn,n) for n in LEGACY_TABLES)
    retire.legacy_tables(apply=True); retire.restore(apply=True)
    assert all(table_snapshot(conn,n)['content_sha256']==snaps[n]['content_sha256'] for n in LEGACY_TABLES)


def test_only_aged_completed_payloads_archive_and_control_results_survive(retired_db):
    conn, db, cfg, retire, snaps, event, manifest = retired_db
    journal = Journal(conn)
    _, control = journal.enqueue({'post_type':'message','group_id':1001,'user_id':2001,'message_id':8002})
    assert journal.group_control(control,1001,'is_chat_mode',True)
    journal.prepare(control,None); journal.finish(control)
    _, pending = journal.enqueue({'post_type':'message','group_id':1001,'user_id':2001,'message_id':8003})
    with conn.cursor() as cur: cur.execute('UPDATE yunying_ingress SET delivered_at=%s WHERE event_id=%s',(datetime.now()-timedelta(days=31),control))
    proof=json.loads(manifest.read_text()); snap=table_snapshot(conn,'yunying_ingress')
    proof['tables']['yunying_ingress']={k:snap[k] for k in ('row_count','content_sha256','schema_sha256')}; manifest.write_text(json.dumps(proof))
    assert retire.ingress(apply=True)['archived_ingress']==2
    assert journal.group_control(control,1001,'is_chat_mode',False)
    with conn.cursor() as cur:
        cur.execute('SELECT payload_archived_at,recording_status,delivery_status FROM yunying_ingress WHERE event_id=%s',(pending,))
        assert cur.fetchone()=={'payload_archived_at':None,'recording_status':'pending','delivery_status':'pending'}
        cur.execute('SELECT COUNT(*) n FROM yunying_events'); assert cur.fetchone()['n']==0
