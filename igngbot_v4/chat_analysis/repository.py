"""Durable daily analysis runs. Every mutation is lease-checked; publication is transactional.

Run states: pending -> preparing (Python snapshot) -> ready -> running (DSH) -> completed | failed.
A day with no member messages is published by Python without any model call.
"""
import json
import uuid
from contextlib import contextmanager
from .period import day_period
from .result import Validator, Invalid, render_markdown, render_empty
from .source import conversation, encode, content_hash, instant, model_view, public_roster

PHASES = {'timeline_scan', 'topic_investigation', 'member_review', 'report_write', 'report_check'}
ACTIVE = ('preparing', 'ready', 'running')
LEASE_SECONDS = 300


def decode(value, fallback=None):
    return json.loads(value) if isinstance(value, (str, bytes)) else value if value is not None else fallback


class Conflict(ValueError):
    pass


def _collect_refs(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'ref' and isinstance(item, str):
                found.add(item)
            elif key in {'evidence_refs', 'summary_refs'} and isinstance(item, list):
                found.update(ref for ref in item if isinstance(ref, str))
            else:
                _collect_refs(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_refs(item, found)
    return found


class Repository:
    def __init__(self, connect, config):
        self.connect = connect
        self.config = config

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.begin()
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ settings
    def settings(self):
        with self.transaction() as cur:
            cur.execute('SELECT platform,conversation_type,external_id,enabled,timezone,updated_by,updated_at '
                        'FROM chat_analysis_settings ORDER BY platform,conversation_type,external_id')
            return [{**row, 'enabled': bool(row['enabled'])} for row in cur.fetchall()]

    def configure(self, platform, conversation_type, external_id, enabled, zone, actor):
        platform, conversation_type, external_id = conversation(platform, conversation_type, external_id)
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be a boolean')
        from zoneinfo import ZoneInfo
        zone = zone or self.config['default_timezone']
        ZoneInfo(zone)
        with self.transaction() as cur:
            cur.execute('INSERT INTO chat_analysis_settings(platform,conversation_type,external_id,enabled,timezone,updated_by) '
                        'VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE enabled=VALUES(enabled),timezone=VALUES(timezone),'
                        'updated_by=VALUES(updated_by)', (platform, conversation_type, external_id, enabled, zone, actor[:80]))
        return {'ok': True, 'platform': platform, 'conversation_type': conversation_type, 'external_id': external_id,
                'enabled': enabled, 'timezone': zone}

    def enabled(self):
        with self.transaction() as cur:
            cur.execute('SELECT platform,conversation_type,external_id,timezone FROM chat_analysis_settings WHERE enabled=TRUE')
            return cur.fetchall()

    # ------------------------------------------------------------------ queue
    def create(self, platform, conversation_type, external_id, label, zone, actor, request_key, retry_of=None):
        platform, conversation_type, external_id = conversation(platform, conversation_type, external_id)
        start, end = day_period(label, zone)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError('invalid idempotency key')
        identity = (platform, conversation_type, external_id, label, zone)
        with self.transaction() as cur:
            cur.execute('SELECT * FROM chat_analysis_runs WHERE request_key=%s FOR UPDATE', (request_key,))
            existing = cur.fetchone()
            if existing:
                cur.execute('SELECT * FROM chat_analysis_reports WHERE id=%s', (existing['report_id'],))
                report = cur.fetchone()
                if (report['platform'], report['conversation_type'], report['external_id'], str(report['period_start']),
                        report['timezone']) != identity:
                    raise Conflict('idempotency key belongs to another report')
                return {'run_id': existing['id'], 'report_id': existing['report_id'], 'status': existing['status']}
            cur.execute('INSERT INTO chat_analysis_reports(id,platform,conversation_type,external_id,period_start,timezone) '
                        'VALUES(%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=id', (str(uuid.uuid4()), *identity))
            cur.execute('SELECT id FROM chat_analysis_reports WHERE platform=%s AND conversation_type=%s AND external_id=%s '
                        'AND period_start=%s AND timezone=%s FOR UPDATE', identity)
            report_id = cur.fetchone()['id']
            if retry_of:
                cur.execute('SELECT status,report_id FROM chat_analysis_runs WHERE id=%s FOR UPDATE', (retry_of,))
                prior = cur.fetchone()
                if not prior or prior['report_id'] != report_id or prior['status'] != 'failed':
                    raise Conflict('only a failed run can be retried')
            config = {k: v for k, v in self.config.items() if k not in {'default_timezone'}}
            run_id = str(uuid.uuid4())
            cur.execute('INSERT INTO chat_analysis_runs(id,report_id,request_key,retry_of,actor,config,period_from,period_to,dsh_session_id) '
                        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (run_id, report_id, request_key, retry_of, actor[:80], encode(config), start, end, str(uuid.uuid4())))
            return {'run_id': run_id, 'report_id': report_id, 'status': 'pending'}

    def retry(self, run_id, actor, request_key):
        with self.transaction() as cur:
            cur.execute('SELECT p.* FROM chat_analysis_reports p JOIN chat_analysis_runs r ON r.report_id=p.id WHERE r.id=%s', (run_id,))
            row = cur.fetchone()
        if not row:
            raise ValueError('run not found')
        return self.create(row['platform'], row['conversation_type'], row['external_id'], str(row['period_start']),
                           row['timezone'], actor, request_key, run_id)

    def _lease(self, cur, frm, to):
        # Locking the oldest queue row serializes competing workers.
        cur.execute('SELECT r.*,p.platform,p.conversation_type,p.external_id,p.timezone,p.period_start,'
                    'p.display_revision report_display_revision FROM chat_analysis_runs r JOIN chat_analysis_reports p ON p.id=r.report_id '
                    'WHERE r.status=%s AND r.available_at<=UTC_TIMESTAMP(6) AND NOT EXISTS (SELECT 1 FROM chat_analysis_runs a '
                    f"WHERE a.report_id=r.report_id AND a.id<>r.id AND a.status IN ({','.join(['%s'] * len(ACTIVE))})"
                    + (" AND a.status<>'ready'" if frm == 'ready' else '') +
                    ') ORDER BY r.created_at,r.id LIMIT 1 FOR UPDATE', (frm, *ACTIVE))
        row = cur.fetchone()
        if not row:
            return None
        token = str(uuid.uuid4())
        cur.execute(f'UPDATE chat_analysis_runs SET status=%s,lease_token=%s,lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL {LEASE_SECONDS} SECOND),'
                    'started_at=COALESCE(started_at,UTC_TIMESTAMP(6)),phase=%s WHERE id=%s AND status=%s',
                    (to, token, 'preparing' if to == 'preparing' else 'timeline_scan', row['id'], frm))
        if cur.rowcount != 1:
            return None
        row.update(status=to, lease_token=token, config=decode(row['config']))
        return row

    def claim_prepare(self):
        with self.transaction() as cur:
            row = self._lease(cur, 'pending', 'preparing')
            if row:
                cur.execute('UPDATE chat_analysis_runs SET display_revision=%s WHERE id=%s', (row['report_display_revision'], row['id']))
            return row

    def claim(self):
        with self.transaction() as cur:
            row = self._lease(cur, 'ready', 'running')
            if not row:
                return None
            manifest = decode(row['manifest'], {})
            return {**{k: row[k] for k in ('id', 'report_id', 'platform', 'conversation_type', 'external_id', 'timezone',
                                            'period_from', 'period_to', 'dsh_session_id', 'lease_token', 'config')},
                    'period_start': str(row['period_start']), 'message_count': manifest.get('message_count', 0),
                    'human_message_count': manifest.get('human_message_count', 0),
                    'participant_count': sum(1 for p in manifest.get('participants', []) if p.get('spoke'))}

    def owned(self, cur, run_id, token, statuses=('running',)):
        cur.execute('SELECT r.*,p.platform,p.conversation_type,p.external_id,p.timezone,p.period_start '
                    'FROM chat_analysis_runs r JOIN chat_analysis_reports p ON p.id=r.report_id '
                    f"WHERE r.id=%s AND r.lease_token=%s AND r.status IN ({','.join(['%s'] * len(statuses))}) "
                    'AND r.lease_until>UTC_TIMESTAMP(6) FOR UPDATE', (run_id, token, *statuses))
        row = cur.fetchone()
        if not row:
            raise Conflict('run lease is unavailable')
        return row

    def heartbeat(self, run_id, token):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token, ('preparing', 'running'))
            cur.execute(f'UPDATE chat_analysis_runs SET lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL {LEASE_SECONDS} SECOND) WHERE id=%s',
                        (run_id,))
            return {'ok': True, 'draft_saved': bool(row['markdown'])}

    def install_snapshot(self, run_id, token, messages, manifest, statistics):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token, ('preparing',))
            if row['manifest']:
                return {'ok': True, 'status': row['status']}  # Evidence is never rewritten.
            cur.execute("SELECT r.id FROM chat_analysis_reports p JOIN chat_analysis_runs r ON r.id=p.current_run_id "
                        "WHERE p.platform=%s AND p.conversation_type=%s AND p.external_id=%s AND p.timezone=%s AND p.period_start<%s "
                        "AND r.status='completed' AND r.analysis='ai' AND r.ended_at<=%s ORDER BY p.period_start DESC LIMIT 7",
                        (row['platform'], row['conversation_type'], row['external_id'], row['timezone'], row['period_start'],
                         manifest['cutoff']))
            manifest = {**manifest, 'context_run_ids': [r['id'] for r in cur.fetchall()]}
            for index in range(0, len(messages), 200):
                cur.executemany('INSERT INTO chat_analysis_messages(run_id,ordinal,ref,occurred_at,sender_alias,payload,content_hash) '
                                'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                                [(run_id, n + 1, m['ref'], instant(m['at']), m['sender'], encode(m), content_hash(m))
                                 for n, m in enumerate(messages[index:index + 200], index)])
            empty = manifest.get('human_message_count', 0) == 0
            if empty:
                cur.execute("UPDATE chat_analysis_runs SET manifest=%s,data_cutoff=%s,statistics=%s,analysis='empty',markdown=%s,"
                            "status='completed',phase='empty_publish',ended_at=UTC_TIMESTAMP(6),lease_token=NULL,lease_until=NULL,"
                            "accounting_reconciled=TRUE WHERE id=%s",
                            (encode(manifest), manifest['cutoff'], encode(statistics), render_empty(self.header(row)), run_id))
                self.display(cur, row)
                return {'ok': True, 'status': 'completed', 'analysis': 'empty'}
            cur.execute("UPDATE chat_analysis_runs SET manifest=%s,data_cutoff=%s,statistics=%s,status='ready',phase='queued',"
                        "lease_token=NULL,lease_until=NULL WHERE id=%s",
                        (encode(manifest), manifest['cutoff'], encode(statistics), run_id))
            return {'ok': True, 'status': 'ready'}

    @staticmethod
    def header(row):
        return f"{row['period_start']} · {row['timezone']}"

    # ------------------------------------------------------------------ worker reads
    def _names(self, manifest):
        return {p['alias']: p['name'] for p in manifest.get('participants', [])}

    def events(self, run_id, token, args, mode='timeline'):
        limit = args.get('limit', 120)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('page limit must be 1..200')
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            manifest = decode(row['manifest'], {})
            after = args.get('after', 0)
            if type(after) is not int or after < 0:
                raise ValueError('invalid message cursor')
            values, clauses = [run_id], ['run_id=%s']
            scan = mode == 'timeline' and not any(args.get(k) for k in ('from', 'to'))
            if mode == 'context':
                cur.execute('SELECT ordinal FROM chat_analysis_messages WHERE run_id=%s AND ref=%s', (run_id, args.get('ref')))
                anchor = cur.fetchone()
                if not anchor:
                    raise ValueError('ref outside this day snapshot')
                clauses.extend(['ordinal>=%s', 'ordinal<=%s'])
                values.extend([max(1, anchor['ordinal'] - limit // 2), anchor['ordinal'] + limit // 2])
            else:
                clauses.append('ordinal>%s')
                values.append(after)
            for key, op in [('from', '>='), ('to', '<')]:
                if args.get(key):
                    bound = instant(args[key])
                    if not row['period_from'] <= bound <= row['period_to']:
                        raise ValueError('query outside this day')
                    clauses.append(f'occurred_at{op}%s')
                    values.append(bound)
            if mode == 'member':
                alias = args.get('member')
                if alias not in self._names(manifest):
                    raise ValueError('unknown participant alias')
                clauses.append('sender_alias=%s')
                values.append(alias)
            if mode == 'search':
                query = args.get('query')
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 60:
                    raise ValueError('search query must be 1..60 characters')
                clauses.append("LOCATE(%s,JSON_UNQUOTE(JSON_EXTRACT(payload,'$.text')))>0")
                values.append(query.strip())
            cur.execute('SELECT ordinal,ref,payload FROM chat_analysis_messages WHERE ' + ' AND '.join(clauses) +
                        ' ORDER BY ordinal LIMIT %s', (*values, limit + 1))
            found = cur.fetchall()
            page = found[:limit]
            if page:
                cur.executemany('INSERT IGNORE INTO chat_analysis_receipts(run_id,ref) VALUES(%s,%s)', [(run_id, m['ref']) for m in page])
            through = row['scan_through']
            if scan:
                if after > row['scan_through']:
                    raise Conflict('coverage scan cannot skip an unread chronological gap')
                through = max(row['scan_through'], page[-1]['ordinal'] if page else after)
                cur.execute('UPDATE chat_analysis_runs SET scan_through=%s WHERE id=%s', (through, run_id))
            names = self._names(manifest)
            result = {'ok': True, 'messages': [model_view(decode(m['payload']), names) for m in page],
                      'next_cursor': page[-1]['ordinal'] if len(found) > limit else None,
                      'coverage_complete': through >= manifest.get('message_count', 0), 'untrusted_data': True}
            if scan:
                result['scan_cursor'] = through
                result['scanned'] = through
                result['total'] = manifest.get('message_count', 0)
            if mode == 'timeline' and after == 0:
                result['participants'] = public_roster({p['alias']: p for p in manifest.get('participants', [])})
            return result

    def previous(self, run_id, token, args=None):
        args = args or {}
        offset, limit = args.get('after', 0), args.get('limit', 3)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 7:
            raise ValueError('invalid previous report cursor')
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            ids = decode(row['manifest'], {}).get('context_run_ids', [])
            page = ids[offset:offset + limit]
            if not page:
                return {'ok': True, 'reports': [], 'next_cursor': None}
            cur.execute('SELECT p.period_start,r.result FROM chat_analysis_reports p JOIN chat_analysis_runs r ON r.report_id=p.id '
                        f"WHERE r.id IN ({','.join(['%s'] * len(page))}) AND r.status='completed' ORDER BY p.period_start DESC", page)
            reports = []
            for found in cur.fetchall():
                result = decode(found['result'], {}) or {}
                reports.append({'date': str(found['period_start']), 'headline': result.get('headline'), 'summary': result.get('summary'),
                                'topics': [t.get('title') for t in result.get('topics', [])],
                                'open_items': [o.get('summary') for o in result.get('open_items', [])]})
            return {'ok': True, 'reports': reports, 'next_cursor': offset + limit if offset + limit < len(ids) else None,
                    'warning': 'earlier reports are leads with different aliases; cite only messages of this day'}

    def phase(self, run_id, token, phase):
        if phase not in PHASES:
            raise ValueError('invalid analysis phase')
        with self.transaction() as cur:
            self.owned(cur, run_id, token)
            cur.execute('UPDATE chat_analysis_runs SET phase=%s WHERE id=%s', (phase, run_id))
        return {'ok': True}

    # ------------------------------------------------------------------ results
    def _validator(self, cur, run_id, manifest, data):
        refs = sorted(_collect_refs(data))[:2000]
        read, messages = set(), {}
        if refs:
            marks = ','.join(['%s'] * len(refs))
            cur.execute(f'SELECT ref FROM chat_analysis_receipts WHERE run_id=%s AND ref IN ({marks})', (run_id, *refs))
            read = {r['ref'] for r in cur.fetchall()}
            cur.execute(f'SELECT ref,payload FROM chat_analysis_messages WHERE run_id=%s AND ref IN ({marks})', (run_id, *refs))
            messages = {r['ref']: decode(r['payload']) for r in cur.fetchall()}
        roster = {p['alias']: p for p in manifest.get('participants', [])}
        return Validator(read & set(messages), roster, messages, manifest.get('human_message_count', 0))

    def save(self, run_id, token, data, final=False):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            manifest = decode(row['manifest'], {})
            report = data.get('report') if isinstance(data, dict) else None
            if final and row['scan_through'] < manifest.get('message_count', 0):
                raise Conflict(f"read the whole day first: scanned {row['scan_through']} of {manifest.get('message_count', 0)} messages")
            try:
                result = self._validator(cur, run_id, manifest, report).validate(report, partial=not final)
            except Invalid as error:
                raise ValueError(str(error)) from None
            if not final:
                draft = {**decode(row['draft'], {}), **result}
                cur.execute('UPDATE chat_analysis_runs SET draft=%s WHERE id=%s', (encode(draft), run_id))
                return {'ok': True, 'draft_sections': sorted(draft)}
            names = self._names(manifest)
            markdown = render_markdown(result, names, self.header(row))
            cur.execute("UPDATE chat_analysis_runs SET result=%s,markdown=%s,analysis='ai',phase='report_check' WHERE id=%s",
                        (encode({**result, 'names': {a: names[a] for a in names}}), markdown, run_id))
            return {'ok': True, 'draft_saved': True, 'requires_native_turn_completion': True}

    def display(self, cur, row):
        # An older run never replaces a newer success, nor a manual selection made meanwhile.
        cur.execute('SELECT current_run_id,display_revision FROM chat_analysis_reports WHERE id=%s FOR UPDATE', (row['report_id'],))
        current = cur.fetchone()
        newer = False
        if current['current_run_id']:
            cur.execute('SELECT created_at FROM chat_analysis_runs WHERE id=%s', (current['current_run_id'],))
            newer = cur.fetchone()['created_at'] > row['created_at']
        if not newer and current['display_revision'] == row['display_revision']:
            cur.execute('UPDATE chat_analysis_reports SET current_run_id=%s,display_revision=display_revision+1 WHERE id=%s',
                        (row['id'], row['report_id']))

    def finish(self, run_id, token, success, error=None):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token, ('preparing', 'running'))
            if success and (row['status'] != 'running' or not row['markdown']):
                raise Conflict('DSH ended without a validated final report')
            cur.execute('UPDATE chat_analysis_runs SET status=%s,error=%s,ended_at=UTC_TIMESTAMP(6),lease_token=NULL,lease_until=NULL'
                        + (",accounting_reconciled=TRUE" if row['status'] == 'preparing' else '') + ' WHERE id=%s',
                        ('completed' if success else 'failed', None if success else (error or 'analysis failed')[:255], run_id))
            if success:
                self.display(cur, row)
            return {'ok': True}

    def select(self, report_id, run_id, expected):
        with self.transaction() as cur:
            cur.execute('SELECT current_run_id FROM chat_analysis_reports WHERE id=%s FOR UPDATE', (report_id,))
            row = cur.fetchone()
            if not row or row['current_run_id'] != expected:
                raise Conflict('display version changed; refresh before selecting')
            cur.execute("SELECT 1 FROM chat_analysis_runs WHERE id=%s AND report_id=%s AND status='completed'", (run_id, report_id))
            if not cur.fetchone():
                raise ValueError('version is not a completed run of this report')
            cur.execute('UPDATE chat_analysis_reports SET current_run_id=%s,display_revision=display_revision+1 WHERE id=%s',
                        (run_id, report_id))
        return {'ok': True}

    def expire(self):
        with self.transaction() as cur:
            cur.execute("UPDATE chat_analysis_runs SET status='failed',error='worker lease expired',ended_at=UTC_TIMESTAMP(6),"
                        "lease_token=NULL WHERE status IN ('preparing','running') AND lease_until<=UTC_TIMESTAMP(6)")

    # ------------------------------------------------------------------ accounting
    def account(self, run_id, token, record, recovery=False):
        with self.transaction() as cur:
            if recovery:
                cur.execute("SELECT * FROM chat_analysis_runs WHERE id=%s AND status IN ('failed','completed') FOR UPDATE", (run_id,))
                row = cur.fetchone()
                if not row:
                    raise Conflict('only terminal runs can reconcile native accounting')
            else:
                row = self.owned(cur, run_id, token)
            seq = record.get('native_event_seq')
            if type(seq) is not int or seq < 0 or record.get('record_kind') not in {'attempt', 'task-end'}:
                raise ValueError('invalid native accounting event')
            session = row['dsh_session_id']
            if not str(record.get('task_key', '')).startswith(f'dsh:{session}:'):
                raise ValueError('accounting session mismatch')
            record_id = f'{session}:{seq}'
            phase = 'context_compaction' if record.get('task_type') == 'dsh_compaction' else 'recovered' if recovery else row['phase']
            record = {**record, 'report_run_id': run_id, 'report_id': row['report_id'], 'phase': phase, 'dsh_session_id': session}
            if record.get('supersedes_seq') is not None:
                record['supersedes_record_id'] = f"{session}:{record['supersedes_seq']}"
            cur.execute('INSERT IGNORE INTO yunying_ai_records(record_id,dsh_session_id,request_seq,payload) VALUES(%s,%s,%s,%s)',
                        (record_id, session, seq, encode(record)))
            cur.execute('INSERT IGNORE INTO chat_analysis_calls(record_id,run_id,phase,task_key) VALUES(%s,%s,%s,%s)',
                        (record_id, run_id, phase, record['task_key']))
            return {'ok': True, 'record_id': record_id}

    def recovery(self, run_id=None):
        with self.transaction() as cur:
            if run_id:
                cur.execute("UPDATE chat_analysis_runs SET accounting_reconciled=TRUE WHERE id=%s AND status IN ('failed','completed')", (run_id,))
                return {'ok': True}
            cur.execute("SELECT id,dsh_session_id,config FROM chat_analysis_runs WHERE status IN ('failed','completed') "
                        "AND accounting_reconciled=FALSE AND ended_at<DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 60 SECOND) "
                        "ORDER BY created_at LIMIT 20")
            return {'ok': True, 'runs': [{**r, 'config': decode(r['config'])} for r in cur.fetchall()]}
