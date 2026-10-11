"""Transactional publication and durable curator queue. No QQ permissions."""
import json
import re
from contextlib import contextmanager
from uuid import uuid4
from .models import (entry, material, text, identifier, digest, STATUSES, choice,
                     auto_publishable)


class Conflict(ValueError):
    pass


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def integer(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError('invalid integer')
    return value


class Repository:
    def __init__(self, connect, config):
        self.connect, self.config = connect, config

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.begin()
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def policy(self):
        with self.transaction() as cur:
            cur.execute('SELECT generation,policy FROM slang_meta WHERE id=1')
            row = cur.fetchone()
            return {'ok': True, 'generation': row['generation'], 'policy': decode(row['policy'])}

    def set_policy(self, values):
        if not isinstance(values, dict) or not set(values) <= {'recognition', 'recommendation', 'maintenance', 'auto_publish'} or any(type(v) is not bool for v in values.values()):
            raise ValueError('invalid policy')
        with self.transaction() as cur:
            cur.execute('SELECT policy FROM slang_meta WHERE id=1 FOR UPDATE')
            policy = {**decode(cur.fetchone()['policy']), **values}
            cur.execute('UPDATE slang_meta SET policy=%s,generation=generation+1 WHERE id=1', (json.dumps(policy),))
        return {'ok': True, 'policy': policy}

    def list(self, status=None, after='', limit=100):
        limit = integer(limit, 1, 200)
        after = text(after, 80, True)
        with self.transaction() as cur:
            if status:
                choice(status, STATUSES)
                cur.execute('SELECT * FROM slang_entries WHERE status=%s AND id>%s ORDER BY id LIMIT %s', (status, after, limit))
            else:
                cur.execute('SELECT * FROM slang_entries WHERE id>%s ORDER BY id LIMIT %s', (after, limit))
            rows = cur.fetchall()
            return {'ok': True, 'entries': [{**decode(r['payload']), 'status': r['status'], 'version': r['current_version']} for r in rows],
                    'after': rows[-1]['id'] if rows else after}

    def versions(self, id):
        with self.transaction() as cur:
            cur.execute('SELECT * FROM slang_versions WHERE entry_id=%s ORDER BY version DESC LIMIT 100', (identifier(id),))
            return {'ok': True, 'versions': [{**r, 'payload': decode(r['payload'])} for r in cur.fetchall()]}

    def _save(self, cur, value, status, expected, actor, reason):
        value = entry(value)
        choice(status, STATUSES)
        expected = integer(expected, 0, 1000000000)
        actor, reason = text(actor, 120), text(reason, 500)
        cur.execute('SELECT * FROM slang_entries WHERE id=%s FOR UPDATE', (value['id'],))
        old = cur.fetchone()
        if (old['current_version'] if old else 0) != expected:
            raise Conflict('version conflict; read current entry first')
        if status == 'published' and not any(s['verified'] and s['excerpt'] for s in value['sources']):
            raise ValueError('publication requires owner-reviewed or retrieved source excerpts')
        version = expected + 1
        payload = json.dumps(value, ensure_ascii=False)
        cur.execute('INSERT INTO slang_entries (id,status,current_version,payload,content_hash) VALUES (%s,%s,%s,%s,%s) '
                    'ON DUPLICATE KEY UPDATE status=VALUES(status),current_version=VALUES(current_version),payload=VALUES(payload),content_hash=VALUES(content_hash),updated_at=UTC_TIMESTAMP(6)',
                    (value['id'], status, version, payload, digest(value)))
        cur.execute('INSERT INTO slang_versions (entry_id,version,payload,status,actor,reason) VALUES (%s,%s,%s,%s,%s,%s)',
                    (value['id'], version, payload, status, actor, reason))
        cur.execute('UPDATE slang_meta SET generation=generation+1 WHERE id=1')
        return {'ok': True, 'entry': {**value, 'status': status, 'version': version}}

    def save(self, value, status, expected, actor, reason):
        with self.transaction() as cur:
            return self._save(cur, value, status, expected, actor, reason)

    def rollback(self, id, version, expected, actor, reason):
        with self.transaction() as cur:
            cur.execute('SELECT payload,status FROM slang_versions WHERE entry_id=%s AND version=%s', (identifier(id), integer(version, 1, 1000000000)))
            old = cur.fetchone()
            if not old:
                raise ValueError('version unavailable')
            return self._save(cur, decode(old['payload']), old['status'], expected, actor, reason)

    def candidates(self):
        with self.transaction() as cur:
            cur.execute("SELECT * FROM slang_candidates WHERE status='candidate' ORDER BY created_at,id LIMIT 200")
            return {'ok': True, 'candidates': [{**r, 'payload': decode(r['payload'])} for r in cur.fetchall()]}

    def auto_publish(self):
        # Exact agreement only, independent configured origins, completed jobs, new IDs only.
        with self.transaction() as cur:
            cur.execute('SELECT policy FROM slang_meta WHERE id=1 FOR UPDATE')
            if not decode(cur.fetchone()['policy']).get('auto_publish'):
                return {'ok': True, 'published': []}
            cur.execute("SELECT c.* FROM slang_candidates c JOIN slang_jobs j ON j.id=c.job_id WHERE c.status='candidate' AND j.status='completed' ORDER BY c.created_at LIMIT 200")
            groups = {}
            for row in cur.fetchall():
                value = decode(row['payload'])
                groups.setdefault(value['id'], []).append((row, value))
            published = []
            for id, candidates in groups.items():
                value = candidates[0][1]
                interpretations = {digest({key: field for key, field in candidate.items() if key not in {'sources', 'verified_at'}}) for _, candidate in candidates}
                if len(interpretations) != 1:
                    continue  # Conflicting meaning, tone, aliases or use policy stays for Owner review.
                merged = {**value, 'risks': list({risk for _, candidate in candidates for risk in candidate['risks']}),
                          'sources': list({digest(s): s for _, candidate in candidates for s in candidate['sources']}.values())[:12]}
                if any(candidate['confidence'] != 'high' for _, candidate in candidates):
                    continue
                if not auto_publishable(merged):
                    continue
                cur.execute('SELECT id FROM slang_entries WHERE id=%s', (id,))
                if cur.fetchone():
                    continue  # Never auto-revise a reviewed or disabled entry.
                self._save(cur, {**merged, 'usage': 'understand_only'}, 'published', 0, 'curator:consensus', 'independent retrieved sources agree; low-risk understanding only')
                for row, _ in candidates:
                    cur.execute("UPDATE slang_candidates SET status='published' WHERE id=%s", (row['id'],))
                published.append(id)
            return {'ok': True, 'published': published}

    def lookup(self, query):
        query = text(query, 200)
        match = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self.transaction() as cur:
            cur.execute("SELECT payload,current_version FROM slang_entries WHERE status='published' AND (JSON_UNQUOTE(JSON_EXTRACT(payload,'$.name')) LIKE %s OR JSON_UNQUOTE(JSON_EXTRACT(payload,'$.meaning')) LIKE %s) ORDER BY id LIMIT 5", (match, match))
            return {'ok': True, 'entries': [{**decode(r['payload']), 'version': r['current_version']} for r in cur.fetchall()], 'untrusted': True}

    def review(self, id, expected, actor, reason, publish=True):
        with self.transaction() as cur:
            cur.execute('SELECT * FROM slang_candidates WHERE id=%s FOR UPDATE', (text(id, 36),))
            candidate = cur.fetchone()
            if not candidate or candidate['status'] != 'candidate':
                raise Conflict('candidate already reviewed or missing')
            result = self._save(cur, decode(candidate['payload']), 'published', expected, actor, reason) if publish else {'ok': True}
            cur.execute('UPDATE slang_candidates SET status=%s WHERE id=%s', ('published' if publish else 'rejected', id))
            return result

    def source_save(self, value):
        from .sources import source_config
        value = source_config(value)
        with self.transaction() as cur:
            cur.execute('INSERT INTO slang_sources (id,config) VALUES (%s,%s) ON DUPLICATE KEY UPDATE config=VALUES(config),next_poll_at=NULL',
                        (value['id'], json.dumps(value)))
        return {'ok': True, 'source': value}

    def sources(self, due=False):
        with self.transaction() as cur:
            cur.execute('SELECT * FROM slang_sources' + (' WHERE next_poll_at IS NULL OR next_poll_at<=UTC_TIMESTAMP(6)' if due else '') + ' ORDER BY id LIMIT 100')
            return {'ok': True, 'sources': [{**r, 'config': decode(r['config'])} for r in cur.fetchall()]}

    def source_polled(self, id, seconds, error=None):
        with self.transaction() as cur:
            cur.execute('UPDATE slang_sources SET next_poll_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND),last_error=%s WHERE id=%s',
                        (seconds, error, id))

    def source_claim(self, id):
        with self.transaction() as cur:
            cur.execute('SELECT id FROM slang_sources WHERE id=%s FOR UPDATE', (id,))
            if not cur.fetchone():
                raise ValueError('configured source required')
            cur.execute('SELECT token FROM slang_source_leases WHERE source_id=%s AND expires_at>UTC_TIMESTAMP(6)', (id,))
            if cur.fetchone():
                return None
            token = str(uuid4())
            cur.execute('INSERT INTO slang_source_leases (source_id,token,expires_at) VALUES (%s,%s,DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 900 SECOND)) ON DUPLICATE KEY UPDATE token=VALUES(token),expires_at=VALUES(expires_at)', (id, token))
            return token

    def source_release(self, id, token):
        with self.transaction() as cur:
            cur.execute('UPDATE slang_source_leases SET expires_at=UTC_TIMESTAMP(6) WHERE source_id=%s AND token=%s', (id, token))

    def create(self, source_id, snapshot):
        snapshot = material(snapshot)
        source_id = identifier(source_id)
        key = digest({'source': source_id, 'material': snapshot['hash'], 'url': snapshot['url']})
        with self.transaction() as cur:
            # Serialize enqueue budgets across sources/processes.
            cur.execute('SELECT id FROM slang_meta WHERE id=1 FOR UPDATE')
            cur.execute('SELECT id FROM slang_jobs WHERE request_key=%s', (key,))
            old = cur.fetchone()
            if old:
                return {'ok': True, 'id': old['id'], 'unchanged': True}
            cur.execute('SELECT COUNT(*) AS n FROM slang_jobs WHERE created_at>=UTC_DATE()')
            if cur.fetchone()['n'] >= self.config['daily_jobs']:
                raise Conflict('daily material budget reached')
            id = str(uuid4())
            cur.execute('INSERT INTO slang_jobs (id,request_key,source_id,material,config) VALUES (%s,%s,%s,%s,%s)',
                        (id, key, source_id, json.dumps(snapshot, ensure_ascii=False), json.dumps(self.config)))
        return {'ok': True, 'id': id}

    def known_url(self, source_id, url):
        with self.transaction() as cur:
            cur.execute("SELECT id FROM slang_jobs WHERE source_id=%s AND JSON_UNQUOTE(JSON_EXTRACT(material,'$.url'))=%s LIMIT 1", (source_id, url))
            return cur.fetchone() is not None

    def jobs(self):
        with self.transaction() as cur:
            cur.execute('SELECT id,source_id,status,attempts,submitted,error,created_at,updated_at FROM slang_jobs ORDER BY created_at DESC LIMIT 100')
            return {'ok': True, 'jobs': cur.fetchall()}

    def expire(self):
        with self.transaction() as cur:
            cur.execute("UPDATE slang_runs r JOIN slang_jobs j ON j.dsh_session_id=r.dsh_session_id SET r.status='recovery' WHERE j.status='running' AND j.lease_until<UTC_TIMESTAMP(6)")
            cur.execute("UPDATE slang_jobs SET status=IF(attempts<3 AND submitted=0,'queued','failed'),lease_token=NULL,lease_until=NULL,error='lease expired',updated_at=UTC_TIMESTAMP(6) WHERE status='running' AND lease_until<UTC_TIMESTAMP(6)")

    def claim(self):
        with self.transaction() as cur:
            cur.execute('SELECT id FROM slang_meta WHERE id=1 FOR UPDATE')
            cur.execute("SELECT COUNT(*) AS n FROM slang_jobs WHERE status='running'")
            if cur.fetchone()['n']:
                return {'ok': True, 'run': None}
            cur.execute("SELECT COUNT(*) AS n FROM slang_runs WHERE status='recovery'")
            if cur.fetchone()['n']:
                return {'ok': True, 'run': None, 'recovery_required': True}
            cur.execute("SELECT * FROM slang_jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE")
            row = cur.fetchone()
            if not row:
                return {'ok': True, 'run': None}
            # UTC run-start date; attempts and unknown usage use that run's frozen budget.
            cur.execute("SELECT COALESCE(SUM(COALESCE(c.tokens,CAST(JSON_UNQUOTE(JSON_EXTRACT(r.config,'$.max_tokens')) AS UNSIGNED))),0) AS tokens FROM slang_calls c JOIN slang_runs r ON r.dsh_session_id=JSON_UNQUOTE(JSON_EXTRACT(c.payload,'$.dsh_session_id')) WHERE r.created_at>=UTC_DATE()")
            if cur.fetchone()['tokens'] + decode(row['config'])['max_tokens'] > self.config['daily_tokens']:
                return {'ok': True, 'run': None, 'budget_exhausted': True}
            token, session = str(uuid4()), str(uuid4())
            cur.execute("UPDATE slang_jobs SET status='running',attempts=attempts+1,dsh_session_id=%s,lease_token=%s,lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 90 SECOND),read_ids='[]',updated_at=UTC_TIMESTAMP(6) WHERE id=%s", (session, token, row['id']))
            cur.execute('INSERT INTO slang_runs (dsh_session_id,job_id,config) VALUES (%s,%s,%s)', (session, row['id'], row['config']))
            return {'ok': True, 'run': {'id': row['id'], 'lease_token': token, 'dsh_session_id': session,
                                       'config': decode(row['config']), 'title': decode(row['material'])['title']}}

    def _owned(self, cur, id, token):
        cur.execute("SELECT * FROM slang_jobs WHERE id=%s AND status='running' AND lease_token=%s AND lease_until>UTC_TIMESTAMP(6) FOR UPDATE", (text(id, 36), text(token, 36)))
        row = cur.fetchone()
        if not row:
            raise Conflict('run lease lost')
        row['material'], row['read_ids'] = decode(row['material']), decode(row['read_ids']) or []
        return row

    def heartbeat(self, id, token):
        with self.transaction() as cur:
            self._owned(cur, id, token)
            cur.execute('UPDATE slang_jobs SET lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 90 SECOND) WHERE id=%s', (id,))
        return {'ok': True}

    def evidence(self, id, token, after=0, limit=12):
        after, limit = integer(after, 0, 120), integer(limit, 1, 24)
        with self.transaction() as cur:
            row = self._owned(cur, id, token)
            evidence = row['material']['segments'][after:after + limit]
            read = list(dict.fromkeys(row['read_ids'] + [s['id'] for s in evidence]))
            cur.execute('UPDATE slang_jobs SET read_ids=%s WHERE id=%s', (json.dumps(read), id))
            return {'ok': True, 'segments': evidence, 'after': after + len(evidence),
                    'total': len(row['material']['segments']), 'untrusted': True}

    def submit(self, id, token, values):
        if not isinstance(values, list) or len(values) > 12:
            raise ValueError('up to 12 candidates per material')
        with self.transaction() as cur:
            row = self._owned(cur, id, token)
            evidence = {s['id']: s for s in row['material']['segments']}
            if set(row['read_ids']) != set(evidence):
                raise ValueError('read all evidence pages before submission')
            if row['submitted']:
                return {'ok': True, 'draft_saved': True, 'unchanged': True}
            for ordinal, value in enumerate(values):
                refs = value.get('evidence_ids', [])
                if not refs or not isinstance(refs, list) or len(refs) > 12 or any(r not in evidence for r in refs):
                    raise ValueError('unknown evidence reference')
                # The worker cannot supply source URLs, verified flags or fabricated quotations.
                candidate = entry({**value, 'sources': [evidence[r] for r in dict.fromkeys(refs)],
                                   'observed_at': None, 'verified_at': None, 'lifecycle': 'unknown'})
                cur.execute('INSERT INTO slang_candidates (id,target_id,job_id,ordinal_value,payload) VALUES (%s,%s,%s,%s,%s)',
                            (str(uuid4()), candidate['id'], id, ordinal, json.dumps(candidate, ensure_ascii=False)))
            cur.execute('UPDATE slang_jobs SET submitted=1 WHERE id=%s', (id,))
        return {'ok': True, 'draft_saved': True, 'count': len(values)}

    def finish(self, id, token, success, error=None):
        with self.transaction() as cur:
            row = self._owned(cur, id, token)
            success = success is True and bool(row['submitted'])
            cur.execute('UPDATE slang_jobs SET status=%s,lease_token=NULL,lease_until=NULL,error=%s,updated_at=UTC_TIMESTAMP(6) WHERE id=%s',
                        ('completed' if success else 'failed', None if success else text(error or 'no validated submission', 120), id))
            cur.execute("UPDATE slang_runs SET status='recovery' WHERE dsh_session_id=%s", (row['dsh_session_id'],))
        return {'ok': True, 'success': success}

    def retry(self, id):
        with self.transaction() as cur:
            cur.execute("UPDATE slang_jobs SET status='queued',error=NULL WHERE id=%s AND status='failed' AND attempts<3 AND submitted=0", (text(id, 36),))
            if cur.rowcount != 1:
                raise Conflict('job cannot be retried')
        return {'ok': True}

    def account(self, id, token, record, session=None):
        # Idempotent raw native records, including unknown usage, never estimated costs.
        seq = integer(record['native_event_seq'], 0, 100000000)
        tokens = (record.get('token_breakdown') or {}).get('total') if record.get('record_kind') == 'attempt' else 0
        if tokens is not None:
            integer(tokens, 0, 100000000)
        with self.transaction() as cur:
            if session:
                cur.execute("SELECT * FROM slang_runs WHERE dsh_session_id=%s AND job_id=%s AND status='recovery' FOR UPDATE", (session, id))
                if not cur.fetchone():
                    raise Conflict('unknown recovery session')
            else:
                row = self._owned(cur, id, token)
                session = row['dsh_session_id']
            if not str(record.get('task_key', '')).startswith('dsh:' + session + ':'):
                raise ValueError('accounting session mismatch')
            if record.get('record_kind') not in {'attempt', 'task-end'}:
                raise ValueError('invalid native record kind')
            record_id = f'{session}:{seq}'
            record = {**record, 'dsh_session_id': session, 'slang_job_id': id}
            if record.get('supersedes_seq') is not None:
                prior = integer(record['supersedes_seq'], 0, 100000000)
                cur.execute('UPDATE slang_calls SET tokens=0 WHERE job_id=%s AND record_id=%s', (id, f'{session}:{prior}'))
                record['supersedes_record_id'] = f'{session}:{prior}'
            cur.execute('INSERT INTO slang_calls (job_id,record_id,tokens,payload) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE tokens=VALUES(tokens),payload=VALUES(payload)',
                        (id, record_id, tokens, json.dumps(record)))
            cur.execute('INSERT IGNORE INTO yunying_ai_records (record_id,dsh_session_id,request_seq,payload) VALUES (%s,%s,%s,%s)', (record_id, session, seq, json.dumps(record)))
        return {'ok': True}

    def recovery(self, session=None):
        with self.transaction() as cur:
            if session:
                cur.execute("UPDATE slang_runs SET status='reconciled' WHERE dsh_session_id=%s AND status='recovery'", (text(session, 36),))
                return {'ok': True}
            cur.execute("SELECT * FROM slang_runs WHERE status='recovery' ORDER BY created_at LIMIT 100")
            return {'ok': True, 'runs': [{**r, 'config': decode(r['config'])} for r in cur.fetchall()]}

    def feedback(self, scope, id, preference):
        if not isinstance(scope, str) or not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}', scope):
            raise ValueError('invalid conversation scope')
        choice(preference, {'neutral', 'prefer', 'avoid'})
        with self.transaction() as cur:
            cur.execute("SELECT id FROM slang_entries WHERE id=%s AND status='published'", (identifier(id),))
            if not cur.fetchone():
                raise ValueError('published entry required')
            # Owner supplied aggregate preference only; no chat prose or user IDs are stored.
            cur.execute('INSERT INTO slang_feedback (scope_key,entry_id,preference) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE preference=VALUES(preference),updated_at=UTC_TIMESTAMP(6)', (scope, id, preference))
            cur.execute('UPDATE slang_meta SET generation=generation+1 WHERE id=1')
        return {'ok': True}

    def consent(self, scope, enabled):
        if not isinstance(scope, str) or not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}', scope) or type(enabled) is not bool:
            raise ValueError('explicit scoped consent required')
        with self.transaction() as cur:
            cur.execute('INSERT INTO slang_scopes (scope_key,enabled) VALUES (%s,%s) ON DUPLICATE KEY UPDATE enabled=VALUES(enabled)', (scope, enabled))
            cur.execute('UPDATE slang_meta SET generation=generation+1 WHERE id=1')
        return {'ok': True}

    def metrics(self):
        with self.transaction() as cur:
            cur.execute('SELECT scope_key,entry_id,preference,observations,updated_at FROM slang_feedback ORDER BY updated_at DESC LIMIT 200')
            feedback = cur.fetchall()
            cur.execute('SELECT COUNT(*) AS records,COALESCE(SUM(tokens),0) AS known_tokens,SUM(tokens IS NULL) AS unknown_usage FROM slang_calls')
            usage = cur.fetchone()
            cur.execute('SELECT kind,COUNT(*) AS calls,SUM(input_units) AS input_units,SUM(known_tokens) AS known_tokens,SUM(known_tokens IS NULL) AS unknown_usage FROM slang_auxiliary_calls GROUP BY kind')
            return {'ok': True, 'feedback': feedback, 'usage': usage, 'auxiliary_usage': cur.fetchall()}

    def reserve_auxiliary(self, kind, model, units, limit):
        choice(kind, {'asr', 'vision', 'embedding'})
        units, limit = integer(units, 1, 32), integer(limit, 1, 10000)
        with self.transaction() as cur:
            cur.execute('SELECT id FROM slang_meta WHERE id=1 FOR UPDATE')
            cur.execute('SELECT COALESCE(SUM(input_units),0) AS used FROM slang_auxiliary_calls WHERE created_at>=UTC_DATE()')
            if cur.fetchone()['used'] + units > limit:
                raise Conflict('daily auxiliary model budget reached')
            id = str(uuid4())
            cur.execute('INSERT INTO slang_auxiliary_calls (id,kind,model,input_units) VALUES (%s,%s,%s,%s)', (id, kind, text(model, 120), units))
            return id

    def finish_auxiliary(self, id, ok, usage):
        tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
        if type(tokens) is not int or not 0 <= tokens <= 100000000:
            tokens = None
        with self.transaction() as cur:
            cur.execute('UPDATE slang_auxiliary_calls SET status=%s,known_tokens=%s WHERE id=%s', ('completed' if ok else 'failed', tokens, id))

    def resolve_unavailable_session(self, session, actor, reason):
        with self.transaction() as cur:
            cur.execute("SELECT * FROM slang_runs WHERE dsh_session_id=%s AND status='recovery' FOR UPDATE", (text(session, 36),))
            row = cur.fetchone()
            if not row:
                raise Conflict('only pending recovery sessions can be reviewed')
            payload = {'record_kind': 'unknown-budget-reservation', 'dsh_session_id': session,
                       'actor': text(actor, 120), 'reason': text(reason, 500)}
            # Conservative run-sized budget debit, NOT fabricated provider usage or a bill.
            cur.execute('INSERT IGNORE INTO slang_calls (job_id,record_id,tokens,payload) VALUES (%s,%s,NULL,%s)', (row['job_id'], session + ':unavailable', json.dumps(payload)))
            cur.execute("UPDATE slang_runs SET status='reviewed-unavailable' WHERE dsh_session_id=%s", (session,))
        return {'ok': True, 'usage_unknown': True, 'budget_reserved': True}

    def semantic_consent(self, scope, enabled=None):
        if not isinstance(scope, str) or not re.fullmatch(r'(?:group|private):[1-9][0-9]{0,19}', scope):
            raise ValueError('invalid conversation scope')
        with self.transaction() as cur:
            if enabled is not None:
                if type(enabled) is not bool:
                    raise ValueError('explicit boolean consent required')
                cur.execute('INSERT INTO slang_semantic_consent (scope_key,enabled) VALUES (%s,%s) ON DUPLICATE KEY UPDATE enabled=VALUES(enabled)', (scope, enabled))
            cur.execute('SELECT enabled FROM slang_semantic_consent WHERE scope_key=%s', (scope,))
            row = cur.fetchone()
            return {'ok': True, 'enabled': bool(row and row['enabled'])}

    def reserve_semantic(self, count, limit):
        with self.transaction() as cur:
            cur.execute('INSERT IGNORE INTO slang_semantic_usage (day_value) VALUES (UTC_DATE())')
            cur.execute('SELECT requests FROM slang_semantic_usage WHERE day_value=UTC_DATE() FOR UPDATE')
            if cur.fetchone()['requests'] + count > limit:
                raise Conflict('daily embedding input budget reached')
            cur.execute('UPDATE slang_semantic_usage SET requests=requests+%s WHERE day_value=UTC_DATE()', (count,))
