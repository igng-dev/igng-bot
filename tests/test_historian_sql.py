"""Opt-in real SQL, fixed loopback and synthetic evidence only."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import pytest
import pymysql
from pymysql.cursors import DictCursor
from igngbot_v4.historian.repository import Repository, Conflict
from igngbot_v4.historian.events import normalize, MCSource

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test', reason='temporary DB opt-in')


def connect():
    return pymysql.connect(host='127.0.0.1',port=int(os.getenv('YUNYING_TEST_DB_PORT','33316')),
                           user='root',password='',database='yunying_v4_test',autocommit=True,
                           cursorclass=DictCursor,init_command="SET time_zone='+00:00'")


def repository():
    return Repository(connect,{'servers':[190001],'zones':{190001:'Asia/Shanghai'},'model':'fixture','provider':'script',
                              'max_steps':20,'max_tokens':10000,'max_seconds':120,'output_tokens':1000})


def start(repo,kind='daily',label='2026-01-05'):
    created=repo.create(190001,kind,label,'Asia/Shanghai','site:fixture',str(uuid.uuid4()))
    # Other suites may leave pending jobs; claim only fixture-created jobs in this isolated suite.
    with repo.transaction() as cur:
        cur.execute('UPDATE historian_runs SET available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 1 DAY) WHERE id<>%s AND status=\'pending\'',(created['run_id'],))
    run=repo.claim()
    assert run['id']==created['run_id']
    event=normalize('chat_messages',{'id':1,'server_id':190001,'sent_at':run['period_from']+timedelta(hours=1),
          'player_name':'Synthetic','player_uuid':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','content':'合成原文',
          'source':'paper','cancelled':False})
    repo.snapshot(run['id'],run['lease_token'],[event],{'cutoff':datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=' '),'event_count':1},{'chat':1})
    return run,event


def final(repo,run,event):
    data={'observations':[{'title':'合成事件','summary':'玩家发言。','certainty':'fact','evidence_event_ids':[event['event_id']]}]}
    repo.events(run['id'],run['lease_token'],{'after':0})
    repo.save(run['id'],run['lease_token'],data,True)
    repo.finish(run['id'],run['lease_token'],True)


def test_report_run_idempotency_evidence_coverage_and_publication():
    repo=repository();run,event=start(repo)
    same=repo.create(190001,'daily','2026-01-05','Asia/Shanghai','site:fixture',run['request_key'])
    assert same['run_id']==run['id']
    with pytest.raises(Conflict):repo.create(190001,'daily','2026-01-06','Asia/Shanghai','site:fixture',run['request_key'])
    data={'observations':[{'title':'观察','summary':'原文','certainty':'fact','evidence_event_ids':[event['event_id']]}]}
    with pytest.raises(ValueError):repo.save(run['id'],run['lease_token'],data,True)
    with pytest.raises(Conflict):repo.events(run['id'],run['lease_token'],{'after':99})
    result=repo.events(run['id'],run['lease_token'],{'after':0,'limit':1})
    assert result['coverage_complete'] and 'statistics' not in result
    assert repo.events(run['id'],run['lease_token'],{'after':0,'limit':1})['events']==result['events']
    assert repo.events(run['id'],run['lease_token'],{'event_id':event['event_id']},'context')['events'][0]['facts']['content']=='合成原文'
    bad={'observations':[{**data['observations'][0],'evidence_event_ids':['mc:190001:chat_messages:999']}]}
    with pytest.raises(ValueError):repo.save(run['id'],run['lease_token'],bad,True)
    bad['observations'][0]['evidence_event_ids']=[event['event_id'].upper()]
    with pytest.raises(ValueError):repo.save(run['id'],run['lease_token'],bad,True)
    repo.save(run['id'],run['lease_token'],data,True);repo.finish(run['id'],run['lease_token'],True)
    with pytest.raises(Conflict):repo.save(run['id'],run['lease_token'],data,True)
    with repo.transaction() as cur:
        cur.execute('SELECT current_run_id FROM historian_reports WHERE id=%s',(run['report_id'],))
        assert cur.fetchone()['current_run_id']==run['id']


def test_failed_regeneration_preserves_version_retry_new_session_and_selection_cas():
    repo=repository();run,event=start(repo,label='2026-01-07');final(repo,run,event)
    failed,_=start(repo,label='2026-01-07');repo.finish(failed['id'],failed['lease_token'],False,'fixture failure')
    retry=repo.retry(failed['id'],'site:fixture',str(uuid.uuid4()))
    with repo.transaction() as cur:
        cur.execute('SELECT dsh_session_id FROM historian_runs WHERE id=%s',(retry['run_id'],))
        assert cur.fetchone()['dsh_session_id']!=failed['dsh_session_id']
        cur.execute('SELECT current_run_id FROM historian_reports WHERE id=%s',(run['report_id'],))
        assert cur.fetchone()['current_run_id']==run['id']
    with pytest.raises(ValueError):repo.select(run['report_id'],failed['id'],run['id'])
    with pytest.raises(Conflict):repo.select(run['report_id'],run['id'],str(uuid.uuid4()))
    repo.select(run['report_id'],run['id'],run['id'])


def test_concurrent_claim_lease_fencing_and_terminal_accounting_recovery():
    repo=repository();run,event=start(repo,label='2026-01-09')
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _:repo.claim(),range(2)))==[None,None]
    with repo.transaction() as cur:
        cur.execute('UPDATE historian_runs SET lease_until=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND) WHERE id=%s',(run['id'],))
    repo.expire()
    with pytest.raises(Conflict):repo.heartbeat(run['id'],run['lease_token'])
    record={'record_kind':'attempt','native_event_seq':3,'task_key':f"dsh:{run['dsh_session_id']}:turn:1",'model':'fixture'}
    repo.account(run['id'],None,record,True);repo.account(run['id'],None,record,True)
    with repo.transaction() as cur:
        cur.execute('SELECT COUNT(*) n FROM historian_calls WHERE run_id=%s',(run['id'],))
        assert cur.fetchone()['n']==1


def test_weekly_can_read_daily_clues_across_its_whole_week():
    repo=repository();daily,event=start(repo,label='2026-01-15');final(repo,daily,event)
    weekly,_=start(repo,kind='weekly',label='2026-01-12')
    try:
        clues=repo.previous(weekly['id'],weekly['lease_token'])['reports']
        assert any(r['id']==daily['report_id'] for r in clues)
        assert all('statistics' not in r for r in clues)
    finally:repo.finish(weekly['id'],weekly['lease_token'],False,'fixture complete')


def test_manual_selection_is_not_overwritten_by_an_inflight_generation():
    repo=repository();first,event=start(repo,label='2026-01-20');final(repo,first,event)
    second,event=start(repo,label='2026-01-20')
    repo.select(first['report_id'],first['id'],first['id'])
    final(repo,second,event)
    with repo.transaction() as cur:
        cur.execute('SELECT current_run_id FROM historian_reports WHERE id=%s',(first['report_id'],))
        assert cur.fetchone()['current_run_id']==first['id']


def test_source_snapshot_keeps_original_context_and_excludes_late_rows_from_old_version():
    server=1800000000+uuid.uuid4().int%1000000
    with connect() as c:
        with c.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS chat_messages (id BIGINT AUTO_INCREMENT PRIMARY KEY,server_id INT,sent_at DATETIME(3),player_uuid VARCHAR(36),player_name VARCHAR(64),content TEXT,source VARCHAR(16),cancelled BOOLEAN)')
            cur.execute('CREATE TABLE IF NOT EXISTS player_event_logs (id BIGINT AUTO_INCREMENT PRIMARY KEY,server_id INT,occurred_at DATETIME(3),player_uuid VARCHAR(36),player_name VARCHAR(64),event_type VARCHAR(32),reason TEXT)')
            for text,minute in [('先问一个问题',1),('随后回答',2)]:
                cur.execute('INSERT INTO chat_messages(server_id,sent_at,player_name,content,source,cancelled) VALUES(%s,%s,%s,%s,\'paper\',0)',
                            (server,datetime(2026,1,1,0,minute,0,123000),'Synthetic',text))
    source=MCSource(connect)
    events,history,manifest=source.snapshot(server,datetime(2026,1,1),datetime(2026,1,2))
    assert [e['facts']['content'] for e in events]==['先问一个问题','随后回答']
    assert events[0]['occurred_at'].endswith('.123Z')
    with connect() as c:
        with c.cursor() as cur:
            cur.execute('INSERT INTO chat_messages(server_id,sent_at,player_name,content,source,cancelled) VALUES(%s,%s,%s,%s,\'paper\',0)',
                        (server,datetime(2026,1,1,0,1,30),'Synthetic','迟到的原始消息'))
    newer,_,new_manifest=source.snapshot(server,datetime(2026,1,1),datetime(2026,1,2))
    assert len(events)==2 and len(newer)==3
    assert int(new_manifest['watermarks']['chat_messages'])>int(manifest['watermarks']['chat_messages'])
