"""Opt-in disposable SQL with synthetic materials. No production config or credentials."""
import os
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import pymysql
from pymysql.cursors import DictCursor
import pytest
from igngbot_v4.slang.repository import Repository, Conflict
from igngbot_v4.slang.service import settings
from igngbot_v4.slang.seed import entries

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test', reason='disposable DB opt-in')


def connect():
    return pymysql.connect(host='127.0.0.1', port=int(os.getenv('YUNYING_TEST_DB_PORT', '33316')), user='root', password='', database='yunying_v4_test', autocommit=True,
                           cursorclass=DictCursor, init_command="SET time_zone='+00:00'")


def repo():
    return Repository(connect, {**settings({}), 'daily_jobs': 100, 'daily_tokens': 2000000, 'max_tokens': 1000})


def snapshot():
    return {'url': 'https://example.com/' + str(uuid4()), 'title': '合成素材', 'origin': 'fixture:original',
            'segments': [{'id': 'segment:1', 'excerpt': '这是合成的公开素材原文，用于验证真实来源引用与事务，不是生产知识。', 'start': 0, 'end': 10}]}


def candidate(id=None):
    value = entries()[0]
    value['id'] = id or 'fixture:' + str(uuid4())
    value['sources'][0].update(verified=True, excerpt='合成审核证据，专供临时数据库的版本契约测试。')
    return value


def start(repository):
    material = snapshot()
    created = repository.create('fixture:source', material)
    assert repository.create('fixture:source', material)['unchanged']
    run = repository.claim()['run']
    assert run and run['id'] == created['id']
    return run


def finish(repository, run, success=False):
    repository.finish(run['id'], run['lease_token'], success, 'SQL fixture completed')
    # This SQL-only fixture had no actual native model Session or usage to reconcile.
    repository.recovery(run['dsh_session_id'])


def test_slang_publication_cas_history_rollback_and_candidate_separation():
    r = repo(); value = candidate()
    first = r.save(value, 'candidate', 0, 'fixture:owner', 'new candidate')
    assert first['entry']['version'] == 1
    second = r.save(value, 'published', 1, 'fixture:owner', 'reviewed synthetic evidence')
    with pytest.raises(Conflict):
        r.save(value, 'disabled', 1, 'fixture:owner', 'stale mutation')
    r.save(value, 'disabled', 2, 'fixture:owner', 'disable fixture')
    rollback = r.rollback(value['id'], 2, 3, 'fixture:owner', 'restore as new version')
    assert rollback['entry']['version'] == 4 and rollback['entry']['status'] == 'published'
    assert len(r.versions(value['id'])['versions']) == 4
    raw = entries()[1]; raw['id'] = 'fixture:' + str(uuid4())
    with pytest.raises(ValueError):
        r.save(raw, 'published', 0, 'fixture:owner', 'unreviewed draft cannot publish')


def test_curator_requires_coverage_and_real_refs_idempotent_submission_preserves_published_entry():
    r = repo(); run = start(r); value = candidate()
    r.save(value, 'published', 0, 'fixture:owner', 'known published explanation')
    proposal = {**value, 'meaning': '新的候选解释，不可直接覆盖现有发布版本。', 'evidence_ids': ['segment:1']}
    try:
        with pytest.raises(ValueError):
            r.submit(run['id'], run['lease_token'], [proposal])
        r.evidence(run['id'], run['lease_token'])
        with pytest.raises(ValueError):
            r.submit(run['id'], run['lease_token'], [{**proposal, 'evidence_ids': ['invented:source']}])
        assert r.submit(run['id'], run['lease_token'], [proposal])['draft_saved']
        assert r.submit(run['id'], run['lease_token'], [proposal])['unchanged']
        rows = [c for c in r.candidates()['candidates'] if c['job_id'] == run['id']]
        assert len(rows) == 1 and rows[0]['payload']['sources'][0]['id'] == 'segment:1'
        assert rows[0]['payload']['lifecycle'] == 'unknown' and rows[0]['payload']['observed_at'] is None
        assert r.versions(value['id'])['versions'][0]['payload']['meaning'] == value['meaning']
        with pytest.raises(Conflict):
            r.review(rows[0]['id'], 0, 'fixture:owner', 'must review current version')
        assert r.review(rows[0]['id'], 1, 'fixture:owner', 'reviewed revision')['entry']['version'] == 2
    finally:
        finish(r, run, True)


def test_single_claim_lease_fencing_native_accounting_and_retry_session_identity():
    r = repo(); run = start(r)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert all(result['run'] is None for result in pool.map(lambda _: r.claim(), range(2)))
        base = {'task_key': 'dsh:' + run['dsh_session_id'] + ':turn:1', 'record_kind': 'attempt', 'native_event_seq': 1, 'token_breakdown': {'total': 10}}
        r.account(run['id'], run['lease_token'], base)
        r.account(run['id'], run['lease_token'], {**base, 'native_event_seq': 2, 'supersedes_seq': 1, 'token_breakdown': {'total': 15}})
        r.account(run['id'], run['lease_token'], {**base, 'native_event_seq': 2, 'supersedes_seq': 1, 'token_breakdown': {'total': 15}})
        with r.transaction() as cur:
            cur.execute('SELECT SUM(tokens) AS total,COUNT(*) AS n FROM slang_calls WHERE job_id=%s', (run['id'],))
            assert cur.fetchone() == {'total': 15, 'n': 2}
            cur.execute('UPDATE slang_jobs SET lease_until=DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 1 SECOND) WHERE id=%s', (run['id'],))
        r.expire()
        with pytest.raises(Conflict):
            r.heartbeat(run['id'], run['lease_token'])
        assert r.claim().get('recovery_required')
        r.account(run['id'], None, base, run['dsh_session_id'])
        r.recovery(run['dsh_session_id'])
        retried = r.claim()['run']
        assert retried['id'] == run['id'] and retried['dsh_session_id'] != run['dsh_session_id']
        run = retried
    finally:
        finish(r, run)


def test_scoped_feedback_consent_and_low_risk_auto_publish_never_rewrites_reviewed_content():
    r = repo(); value = candidate()
    r.save(value, 'published', 0, 'fixture:owner', 'fixture publication')
    r.feedback('group:1001', value['id'], 'avoid')
    r.consent('group:1001', True)
    with r.transaction() as cur:
        cur.execute('SELECT preference FROM slang_feedback WHERE scope_key=%s AND entry_id=%s', ('group:1001', value['id']))
        assert cur.fetchone()['preference'] == 'avoid'
        cur.execute('SELECT preference FROM slang_feedback WHERE scope_key=%s AND entry_id=%s', ('group:1002', value['id']))
        assert cur.fetchone() is None
    r.consent('group:1001', False)
    # A single original source never automatically promotes, even with high self confidence.
    run = start(r)
    try:
        proposal = {**candidate(), 'confidence': 'high', 'evidence_ids': ['segment:1']}
        r.evidence(run['id'], run['lease_token']); r.submit(run['id'], run['lease_token'], [proposal])
    finally:
        finish(r, run, True)
    assert proposal['id'] not in r.auto_publish()['published']


def test_auxiliary_budget_source_lease_and_unavailable_session_owner_reservation():
    r = repo()
    source = {'id': 'fixture:' + str(uuid4()), 'creator_id': 'original', 'feed_url': 'https://example.com/feed', 'allowed_hosts': ['example.com'], 'enabled': False}
    r.source_save(source)
    token = r.source_claim(source['id']); assert token
    assert r.source_claim(source['id']) is None
    r.source_release(source['id'], token)
    assert r.source_claim(source['id'])
    aux = r.reserve_auxiliary('embedding', 'fixture', 2, 10000)
    r.finish_auxiliary(aux, True, {'total_tokens': 12})
    with pytest.raises(Conflict):
        r.reserve_auxiliary('embedding', 'fixture', 1, 1)
    r.semantic_consent('group:1001', True)
    assert r.semantic_consent('group:1001')['enabled']
    assert not r.semantic_consent('group:1002')['enabled']
    r.semantic_consent('group:1001', False)
    run = start(r)
    r.finish(run['id'], run['lease_token'], False, 'fixture Session deliberately absent')
    result = r.resolve_unavailable_session(run['dsh_session_id'], 'fixture:owner', 'reviewed synthetic absent Session; no provider calls')
    assert result['usage_unknown']


def test_auto_publication_needs_full_independent_consensus_and_never_claims_fresh_usage():
    r = repo(); target = candidate()
    target.update(confidence='high', usage='common', lifecycle='active', observed_at='2026-01-01T00:00:00Z')
    r.set_policy({'auto_publish': True})
    try:
        for index in range(2):
            snapshot_value = snapshot()
            snapshot_value['origin'] = f'fixture:independent-{index}'
            snapshot_value['segments'][0]['excerpt'] += f'独立原创材料编号{index}。'
            created = r.create('fixture:consensus', snapshot_value)
            run = r.claim()['run']; assert run['id'] == created['id']
            try:
                r.evidence(run['id'], run['lease_token'])
                r.submit(run['id'], run['lease_token'], [{**target, 'evidence_ids': ['segment:1']}])
            finally:
                finish(r, run, True)
        assert target['id'] in r.auto_publish()['published']
        published = r.versions(target['id'])['versions'][0]['payload']
        assert published['usage'] == 'understand_only' and published['lifecycle'] == 'unknown'
        assert published['observed_at'] is None
        assert target['id'] not in r.auto_publish()['published']
        conflicted = candidate(); conflicted['confidence'] = 'high'
        for index in range(3):
            snapshot_value = snapshot(); snapshot_value['origin'] = f'fixture:conflict-{index}'
            snapshot_value['segments'][0]['excerpt'] += f'独立原文编号{index}。'
            r.create('fixture:conflict', snapshot_value)
            run = r.claim()['run']
            try:
                r.evidence(run['id'], run['lease_token'])
                r.submit(run['id'], run['lease_token'], [{**conflicted, 'tone': '可能贬损' if index == 2 else '中性', 'evidence_ids': ['segment:1']}])
            finally:
                finish(r, run, True)
        assert conflicted['id'] not in r.auto_publish()['published']
    finally:
        r.set_policy({'auto_publish': False})
