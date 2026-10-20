"""Opt-in real SQL, fixed loopback and synthetic chat only."""
import os
import uuid
from datetime import datetime, timedelta
import pytest
import pymysql
from pymysql.cursors import DictCursor
from igngbot_v4.chat_analysis.repository import Repository, Conflict
from igngbot_v4.chat_analysis.service import settings
from igngbot_v4.chat_analysis.source import QQGroupSource
from igngbot_v4.chat_analysis.statistics import compute

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test', reason='temporary DB opt-in')
BOT = 3999999999


def connect():
    return pymysql.connect(host='127.0.0.1', port=int(os.getenv('YUNYING_TEST_DB_PORT', '33316')), user='root', password='',
                           database='yunying_v4_test', autocommit=True, cursorclass=DictCursor, init_command="SET time_zone='+00:00'")


def repository():
    return Repository(connect, {**settings({}), 'provider': 'script', 'model': 'fixture', 'max_steps': 20})


def group_with(messages, label='2026-01-05'):
    """Insert synthetic rows for a fresh group; minutes are offsets from local midnight (UTC+8)."""
    group = 1700000000 + uuid.uuid4().int % 100000000
    midnight = datetime.fromisoformat(label) - timedelta(hours=8)
    with connect() as c, c.cursor() as cur:
        cur.execute('INSERT INTO group_configs(group_id,group_name) VALUES(%s,%s)', (group, '合成群'))
        for n, (sender, text, minute, extra) in enumerate(messages):
            cur.execute('INSERT INTO message_logs(group_id,sender_id,sender_name,message_content,plain_text_content,message_structure,'
                        'reply_to_msg_id,msg_id,created_at,is_self,is_recalled) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (group, sender, extra.get('name'), text, text, '[]', extra.get('reply'), f'fixture-{n}',
                         midnight + timedelta(minutes=minute), 1 if sender == BOT else 0, 1 if extra.get('recalled') else 0))
    return str(group)


def isolate(repo, run_id):
    # Other tests leave queued runs; claim only the run this test created.
    with repo.transaction() as cur:
        cur.execute("UPDATE chat_analysis_runs SET available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 1 DAY) "
                    "WHERE id<>%s AND status IN ('pending','ready')", (run_id,))
        cur.execute('UPDATE chat_analysis_runs SET available_at=UTC_TIMESTAMP(6) WHERE id=%s', (run_id,))


def prepared(repo, group, label='2026-01-05'):
    created = repo.create('qq', 'group', group, label, 'Asia/Shanghai', 'site:fixture', str(uuid.uuid4()))
    isolate(repo, created['run_id'])
    row = repo.claim_prepare()
    assert row['id'] == created['run_id']
    source = QQGroupSource(connect, BOT)
    messages, context, manifest = source.snapshot(group, label, 'Asia/Shanghai', row['period_from'], row['period_to'])
    statistics = compute(messages, context, manifest, row['period_from'], row['period_to'], 'Asia/Shanghai')
    installed = repo.install_snapshot(row['id'], row['lease_token'], messages, manifest, statistics)
    return created, installed, messages


def running(repo, created):
    isolate(repo, created['run_id'])
    job = repo.claim()
    assert job['id'] == created['run_id']
    return job


def full_report(messages):
    first, reply = messages[0]['ref'], messages[1]['ref']
    return {'headline': '约局',
            'highlights': [{'text': '有人约开黑', 'evidence_refs': [first]}],
            'topics': [{'title': '开黑', 'gist': '阿青约开黑，小白答应。', 'points': [{'text': '阿青约九点开黑', 'evidence_refs': [first, reply]}],
                        'outcome': {'status': 'settled', 'text': '九点开黑'}, 'participants': ['u1', 'u2'], 'certainty': 'fact'}],
            'quotes': [{'ref': reply, 'context': '小白在回应阿青'}],
            'titles': [{'member': 'u1', 'title': '组局发起人', 'reason': '先喊人', 'evidence_refs': [first]}],
            'open_items': [], 'slang': []}


def test_full_day_snapshot_evidence_coverage_and_publication():
    group = group_with([(10001, '今晚开黑吗', 60, {'name': '阿青'}), (10002, '来！', 61, {'name': '小白', 'reply': 'fixture-0'}),
                        (10003, '秘密', 62, {'recalled': True}), (BOT, '我看着', 63, {}),
                        (10001, '昨天的消息', -5, {'name': '阿青'})])
    repo = repository()
    created, installed, messages = prepared(repo, group)
    assert installed['status'] == 'ready'
    assert [m['text'] for m in messages] == ['今晚开黑吗', '来！', '我看着']
    assert messages[1]['reply_to']['to'] == 'u1'
    same = repo.create('qq', 'group', group, '2026-01-05', 'Asia/Shanghai', 'site:fixture', _request_key(repo, created['run_id']))
    assert same['run_id'] == created['run_id']
    job = running(repo, created)
    assert job['human_message_count'] == 2 and job['participant_count'] == 3
    data = full_report(messages)
    with pytest.raises(Conflict, match='read the whole day'):
        repo.save(job['id'], job['lease_token'], {'report': data}, True)
    with pytest.raises(Conflict):
        repo.events(job['id'], job['lease_token'], {'after': 5})
    page = repo.events(job['id'], job['lease_token'], {'after': 0, 'limit': 2})
    assert page['participants'] and not page['coverage_complete'] and page['next_cursor'] == 2
    assert all('10001' not in str(m) for m in page['messages'])
    with pytest.raises(ValueError, match='refs must be real'):
        repo.save(job['id'], job['lease_token'], {'report': {**data, 'highlights': [{'text': '云萤看着', 'evidence_refs': [messages[2]['ref']]}]}}, False)
    rest = repo.events(job['id'], job['lease_token'], {'after': page['scan_cursor']})
    assert rest['coverage_complete']
    assert repo.events(job['id'], job['lease_token'], {'member': 'u2'}, 'member')['messages'][0]['text'] == '来！'
    assert repo.events(job['id'], job['lease_token'], {'query': '开黑'}, 'search')['messages'][0]['ref'] == messages[0]['ref']
    assert repo.save(job['id'], job['lease_token'], {'report': {'topics': data['topics']}}, False)['draft_sections'] == ['topics']
    repo.save(job['id'], job['lease_token'], {'report': data}, True)
    repo.finish(job['id'], job['lease_token'], True)
    with repo.transaction() as cur:
        cur.execute('SELECT p.current_run_id,r.result,r.markdown,r.statistics FROM chat_analysis_reports p JOIN chat_analysis_runs r '
                    'ON r.id=p.current_run_id WHERE p.id=%s', (created['report_id'],))
        row = cur.fetchone()
    assert row['current_run_id'] == created['run_id']
    assert '小白' in row['markdown'] and '来！' in row['markdown'] and '组局发起人' in row['markdown']
    assert '"recalled":1' in row['statistics'].replace(' ', '')


def _request_key(repo, run_id):
    with repo.transaction() as cur:
        cur.execute('SELECT request_key FROM chat_analysis_runs WHERE id=%s', (run_id,))
        return cur.fetchone()['request_key']


def test_empty_day_is_published_without_the_model_and_bot_only_counts_as_empty():
    group = group_with([(BOT, '有人吗', 30, {}), (10001, '撤回了', 31, {'recalled': True})])
    repo = repository()
    created, installed, _ = prepared(repo, group)
    assert installed == {'ok': True, 'status': 'completed', 'analysis': 'empty'}
    with repo.transaction() as cur:
        cur.execute('SELECT p.current_run_id,r.accounting_reconciled,r.markdown FROM chat_analysis_reports p '
                    'JOIN chat_analysis_runs r ON r.id=%s WHERE p.id=%s', (created['run_id'], created['report_id']))
        row = cur.fetchone()
    assert row['current_run_id'] == created['run_id'] and row['accounting_reconciled'] == 1 and '安静' in row['markdown']


def test_failed_regeneration_keeps_version_retry_new_session_selection_cas_and_previous_leads():
    group = group_with([(10001, '第一天', 60, {'name': '阿青'}), (10002, '收到', 61, {'name': '小白'})], '2026-01-07')
    with connect() as c, c.cursor() as cur:
        midnight = datetime(2026, 1, 7, 16)
        for n, text in enumerate(['第二天', '继续']):
            cur.execute('INSERT INTO message_logs(group_id,sender_id,sender_name,message_content,plain_text_content,message_structure,msg_id,created_at)'
                        ' VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', (group, 10001 + n, None, text, text, '[]', f'day2-{n}', midnight + timedelta(hours=1, minutes=n)))
    repo = repository()
    created, _, messages = prepared(repo, group, '2026-01-07')
    job = running(repo, created)
    repo.events(job['id'], job['lease_token'], {'after': 0})
    repo.save(job['id'], job['lease_token'], {'report': full_report(messages)}, True)
    repo.finish(job['id'], job['lease_token'], True)
    failed, _, _ = prepared(repo, group, '2026-01-07')
    job2 = running(repo, failed)
    repo.finish(job2['id'], job2['lease_token'], False, 'fixture failure')
    retry = repo.retry(failed['run_id'], 'site:fixture', str(uuid.uuid4()))
    with repo.transaction() as cur:
        cur.execute('SELECT dsh_session_id FROM chat_analysis_runs WHERE id IN (%s,%s)', (retry['run_id'], failed['run_id']))
        assert len({r['dsh_session_id'] for r in cur.fetchall()}) == 2
        cur.execute('SELECT current_run_id FROM chat_analysis_reports WHERE id=%s', (created['report_id'],))
        assert cur.fetchone()['current_run_id'] == created['run_id']
        cur.execute("UPDATE chat_analysis_runs SET available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 1 DAY) WHERE id=%s", (retry['run_id'],))
    with pytest.raises(ValueError):
        repo.select(created['report_id'], failed['run_id'], created['run_id'])
    with pytest.raises(Conflict):
        repo.select(created['report_id'], created['run_id'], str(uuid.uuid4()))
    repo.select(created['report_id'], created['run_id'], created['run_id'])
    next_day, _, _ = prepared(repo, group, '2026-01-08')
    job3 = running(repo, next_day)
    leads = repo.previous(job3['id'], job3['lease_token'])['reports']
    assert leads and leads[0]['date'] == '2026-01-07' and leads[0]['headline'] == '约局'
    repo.finish(job3['id'], job3['lease_token'], False, 'fixture complete')


def test_lease_fencing_expiry_and_terminal_accounting_recovery():
    group = group_with([(10001, '在吗', 60, {'name': '阿青'})], '2026-01-09')
    repo = repository()
    created, _, _ = prepared(repo, group, '2026-01-09')
    job = running(repo, created)
    with pytest.raises(Conflict):
        repo.heartbeat(job['id'], str(uuid.uuid4()))
    with repo.transaction() as cur:
        cur.execute('UPDATE chat_analysis_runs SET lease_until=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND) WHERE id=%s', (job['id'],))
    repo.expire()
    with pytest.raises(Conflict):
        repo.heartbeat(job['id'], job['lease_token'])
    record = {'record_kind': 'attempt', 'native_event_seq': 3, 'task_key': f"dsh:{job['dsh_session_id']}:turn:1", 'model': 'fixture'}
    repo.account(job['id'], None, record, True)
    repo.account(job['id'], None, record, True)
    with pytest.raises(ValueError):
        repo.account(job['id'], None, {**record, 'task_key': 'dsh:other:turn:1'}, True)
    with repo.transaction() as cur:
        cur.execute('SELECT COUNT(*) n FROM chat_analysis_calls WHERE run_id=%s', (job['id'],))
        assert cur.fetchone()['n'] == 1


def test_settings_toggle_and_scheduler_source_of_truth():
    group = group_with([(10001, '你好', 10, {'name': '阿青'})], '2026-01-11')
    repo = repository()
    repo.configure('qq', 'group', group, True, 'Asia/Shanghai', 'site:fixture')
    assert any(r['external_id'] == group for r in repo.enabled())
    repo.configure('qq', 'group', group, False, None, 'site:fixture')
    assert not any(r['external_id'] == group for r in repo.enabled())
    assert QQGroupSource(connect, BOT).known(group) and not QQGroupSource(connect, BOT).known('1999999999999999')
