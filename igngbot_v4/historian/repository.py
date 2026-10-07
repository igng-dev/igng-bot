"""Durable report runs. Every mutation is lease-checked; publication is transactional."""
import json
import uuid
from contextlib import contextmanager
from datetime import timedelta
from .events import encode, content_hash, instant
from .period import period


PHASES = {'timeline_scan', 'event_investigation', 'context_review', 'daily_write', 'weekly_synthesis', 'report_check'}


def decode(value, fallback=None):
    return json.loads(value) if isinstance(value, (str, bytes)) else value if value is not None else fallback


class Conflict(ValueError):
    pass


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

    def create(self, server_id, kind, label, zone, actor, request_key, retry_of=None):
        start, end = period(kind, label, zone)
        if server_id not in self.config['servers']:
            raise ValueError('server outside historian allowlist')
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
            raise ValueError('invalid idempotency key')
        with self.transaction() as cur:
            cur.execute('SELECT * FROM historian_runs WHERE request_key=%s FOR UPDATE', (request_key,))
            existing = cur.fetchone()
            if existing:
                cur.execute('SELECT * FROM historian_reports WHERE id=%s', (existing['report_id'],))
                report = cur.fetchone()
                if (report['server_id'], report['kind'], str(report['period_start']), report['timezone']) != (server_id, kind, label, zone):
                    raise Conflict('idempotency key belongs to another report')
                return {'run_id': existing['id'], 'report_id': existing['report_id'], 'status': existing['status']}
            report_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
            cur.execute('INSERT INTO historian_reports(id,server_id,kind,period_start,timezone) VALUES(%s,%s,%s,%s,%s) '
                        'ON DUPLICATE KEY UPDATE id=id', (report_id, server_id, kind, label, zone))
            cur.execute('SELECT id FROM historian_reports WHERE server_id=%s AND kind=%s AND period_start=%s AND timezone=%s FOR UPDATE',
                        (server_id, kind, label, zone))
            report_id = cur.fetchone()['id']
            if retry_of:
                cur.execute('SELECT status,report_id FROM historian_runs WHERE id=%s FOR UPDATE', (retry_of,))
                prior = cur.fetchone()
                if not prior or prior['report_id'] != report_id or prior['status'] != 'failed':
                    raise Conflict('only a failed run can be retried')
            config = {k: v for k, v in self.config.items() if k not in {'servers', 'zones'}}
            cur.execute('INSERT INTO historian_runs(id,report_id,request_key,retry_of,actor,config,period_from,period_to,dsh_session_id) '
                        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (run_id, report_id, request_key, retry_of, actor[:80], encode(config), start, end, str(uuid.uuid4())))
            return {'run_id': run_id, 'report_id': report_id, 'status': 'pending'}

    def claim(self):
        with self.transaction() as cur:
            # Locking the oldest queue row serializes competing workers, without reusing QQ's runtime lock.
            cur.execute("SELECT r.*,p.server_id,p.kind,p.timezone,p.period_start,p.display_revision report_display_revision FROM historian_runs r JOIN historian_reports p ON p.id=r.report_id "
                        "WHERE r.status='pending' AND r.available_at<=UTC_TIMESTAMP(6) AND NOT EXISTS "
                        "(SELECT 1 FROM historian_runs active WHERE active.report_id=r.report_id AND active.status='running') "
                        f"AND p.server_id IN ({','.join(['%s']*len(self.config['servers']))}) "
                        "ORDER BY r.created_at,r.id LIMIT 1 FOR UPDATE",self.config['servers'])
            row = cur.fetchone()
            if not row:
                return None
            token = str(uuid.uuid4())
            cur.execute("UPDATE historian_runs SET status='running',lease_token=%s,lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 300 SECOND),"
                        "started_at=UTC_TIMESTAMP(6),display_revision=%s WHERE id=%s AND status='pending'", (token,row['report_display_revision'], row['id']))
            if cur.rowcount != 1:
                return None
            row.update(status='running', lease_token=token, config=decode(row['config']))
            return row

    def owned(self, cur, run_id, token):
        cur.execute("SELECT r.*,p.server_id,p.kind,p.timezone,p.period_start FROM historian_runs r JOIN historian_reports p ON p.id=r.report_id "
                    "WHERE r.id=%s AND r.lease_token=%s AND r.status='running' AND r.lease_until>UTC_TIMESTAMP(6) FOR UPDATE", (run_id, token))
        row = cur.fetchone()
        if not row:
            raise Conflict('run lease is unavailable')
        return row

    def heartbeat(self, run_id, token):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            cur.execute('UPDATE historian_runs SET lease_until=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 300 SECOND) WHERE id=%s', (run_id,))
            return {'ok': True, 'draft_saved': bool(row['markdown'])}

    def snapshot(self, run_id, token, events, manifest, statistics):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            if row['manifest']:
                return  # A retry of snapshot installation never rewrites evidence.
            context_end = row['period_start'] + timedelta(days=7) if row['kind']=='weekly' else row['period_start']
            cur.execute("SELECT r.id FROM historian_reports p JOIN historian_runs r ON r.id=p.current_run_id "
                        "WHERE p.server_id=%s AND p.timezone=%s AND p.period_start<%s AND p.id<>%s "
                        "AND r.status='completed' AND r.ended_at<=%s ORDER BY p.period_start DESC,p.id DESC LIMIT 21",
                        (row['server_id'],row['timezone'],context_end,row['report_id'],manifest['cutoff']))
            manifest = {**manifest,'context_run_ids':[r['id'] for r in cur.fetchall()]}
            for index in range(0, len(events), 200):
                cur.executemany('INSERT INTO historian_events(run_id,ordinal,event_id,occurred_at,player_uuid,payload,content_hash) '
                                'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                                [(run_id, n + 1, event['event_id'], instant(event['occurred_at']), event.get('player_uuid'),
                                  encode(event), content_hash(event)) for n, event in enumerate(events[index:index+200], index)])
            cur.execute('UPDATE historian_runs SET manifest=%s,data_cutoff=%s,statistics=%s WHERE id=%s',
                        (encode(manifest), manifest['cutoff'], encode(statistics), run_id))

    def events(self, run_id, token, args, mode='timeline'):
        limit = args.get('limit', 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError('page limit must be 1..200')
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            manifest = decode(row['manifest'])
            if not manifest:
                raise Conflict('snapshot not ready')
            values, clauses = [run_id], ['run_id=%s']
            scan = mode == 'timeline' and not any(args.get(k) for k in ('from', 'to'))
            after = args.get('after', 0)
            if not isinstance(after, int) or isinstance(after, bool) or after < 0:
                raise ValueError('invalid event cursor')
            if mode == 'context':
                cur.execute('SELECT ordinal FROM historian_events WHERE run_id=%s AND event_id=%s', (run_id, args.get('event_id')))
                anchor = cur.fetchone()
                if not anchor:
                    raise ValueError('event outside current snapshot')
                clauses.extend(['ordinal>=%s', 'ordinal<=%s'])
                values.extend([max(1, anchor['ordinal'] - limit // 2), anchor['ordinal'] + limit // 2])
            else:
                clauses.append('ordinal>%s')
                values.append(after)
            for key, op in [('from', '>='), ('to', '<')]:
                if args.get(key):
                    bound = instant(args[key])
                    if not row['period_from'] <= bound <= row['period_to']:
                        raise ValueError('query outside report period')
                    clauses.append(f'occurred_at{op}%s')
                    values.append(bound)
            if mode == 'player':
                uid = str(uuid.UUID(args.get('player_uuid', '')))
                clauses.append('(player_uuid=%s OR JSON_UNQUOTE(JSON_EXTRACT(payload,\'$.facts.related_uuid\'))=%s)')
                values.extend([uid, uid])
            if mode == 'search':
                query = args.get('query')
                if not isinstance(query, str) or not 1 <= len(query) <= 120:
                    raise ValueError('invalid chat search')
                clauses.extend(["JSON_UNQUOTE(JSON_EXTRACT(payload,'$.type'))='CHAT'",
                                "LOCATE(%s,JSON_UNQUOTE(JSON_EXTRACT(payload,'$.facts.content')))>0"])
                values.append(query)
            cur.execute('SELECT ordinal,event_id,payload FROM historian_events WHERE ' + ' AND '.join(clauses) + ' ORDER BY ordinal LIMIT %s',
                        (*values, limit + 1))
            found = cur.fetchall()
            page = found[:limit]
            if page:
                cur.executemany('INSERT IGNORE INTO historian_receipts(run_id,event_id) VALUES(%s,%s)', [(run_id, e['event_id']) for e in page])
            if scan:
                if after > row['scan_through']:
                    raise Conflict('coverage scan cannot skip an unread chronological gap')
                through = max(row['scan_through'], page[-1]['ordinal'] if page else after)
                cur.execute('UPDATE historian_runs SET scan_through=%s WHERE id=%s', (through, run_id))
            return {'ok': True, 'events': [decode(e['payload']) for e in page],
                    'next_cursor': page[-1]['ordinal'] if len(found) > limit else None,
                    'scan_cursor': page[-1]['ordinal'] if scan and page else row['scan_through'],
                    'coverage_complete': (max(row['scan_through'],page[-1]['ordinal']) if scan and page else row['scan_through']) >= manifest['event_count'],
                    'untrusted_data': True}

    def previous(self, run_id, token, args=None):
        args = args or {}
        offset, limit = args.get('after',0), args.get('limit',3)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=7:
            raise ValueError('invalid previous report cursor')
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            ids = decode(row['manifest'],{}).get('context_run_ids',[])
            page = ids[offset:offset+limit]
            if not page:
                return {'ok':True,'reports':[],'next_cursor':None}
            cur.execute("SELECT p.id,p.period_start,p.kind,r.id run_id,r.markdown,r.observations FROM historian_reports p "
                        "JOIN historian_runs r ON r.report_id=p.id AND r.status='completed' "
                        f"WHERE r.id IN ({','.join(['%s']*len(page))}) ORDER BY p.period_start DESC,p.id DESC", page)
            return {'ok': True, 'reports': [{**r, 'observations': decode(r['observations'], [])} for r in cur.fetchall()],
                    'next_cursor':offset+limit if offset+limit<len(ids) else None,
                    'warning': 'previous prose is a lead, not a replacement for original evidence'}

    def observations(self, cur, run_id, values):
        if not isinstance(values, list) or len(values) > 80:
            raise ValueError('invalid observations')
        result = []
        for item in values:
            if not isinstance(item, dict) or item.get('certainty') not in {'fact', 'inference', 'speculation'}:
                raise ValueError('observation requires a certainty classification')
            title, summary, refs = item.get('title'), item.get('summary'), item.get('evidence_event_ids')
            if not isinstance(title, str) or not 1 <= len(title) <= 200 or not isinstance(summary, str) or not 1 <= len(summary) <= 6000:
                raise ValueError('invalid observation text')
            if (not isinstance(refs, list) or not 1 <= len(refs) <= 50 or
                    any(not isinstance(ref,str) or not 1<=len(ref)<=128 for ref in refs) or len(set(refs)) != len(refs)):
                raise ValueError('observation requires distinct raw evidence')
            placeholders = ','.join(['%s'] * len(refs))
            cur.execute(f'SELECT event_id FROM historian_receipts WHERE run_id=%s AND event_id IN ({placeholders})', (run_id, *refs))
            if {r['event_id'] for r in cur.fetchall()} != set(refs):
                raise ValueError('evidence must be real, in scope and read by this run')
            result.append({'title': title, 'summary': summary, 'certainty': item['certainty'], 'evidence_event_ids': refs})
        return result

    def save(self, run_id, token, data, final=False):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            observations = self.observations(cur, run_id, data.get('observations'))
            if not final:
                cur.execute('UPDATE historian_runs SET observations=%s WHERE id=%s', (encode(observations), run_id))
                return {'ok': True}
            manifest = decode(row['manifest'], {})
            if row['scan_through'] < manifest.get('event_count', 1):
                raise Conflict('full chronological coverage is required before final submission')
            # Markdown is generated from validated structured observations, not an unreferenced essay.
            intro = f"服务器 {row['server_id']} · {row['period_start']} · {row['timezone']}"
            if manifest.get('event_count', 0) and not observations:
                raise ValueError('nonempty period needs evidence-backed observations')
            labels = {'fact': '明确事实', 'inference': '有依据的推断', 'speculation': '不确定的推测'}
            markdown = intro + '\n\n' + '\n\n'.join(f"## {o['title']}\n\n[{labels[o['certainty']]}] {o['summary']}\n\n证据："+
                                                     ', '.join(o['evidence_event_ids']) for o in observations)
            if not observations:
                markdown += '本周期可用记录中没有可供调查的服务器事件；这不证明服务器没有活动。'
            cur.execute('UPDATE historian_runs SET observations=%s,markdown=%s,phase=\'report_check\' WHERE id=%s',
                        (encode(observations), markdown, run_id))
            return {'ok': True, 'draft_saved': True, 'requires_native_turn_completion': True}

    def finish(self, run_id, token, success, error=None):
        with self.transaction() as cur:
            row = self.owned(cur, run_id, token)
            if success and not row['markdown']:
                raise Conflict('DSH ended without a validated final report')
            cur.execute('UPDATE historian_runs SET status=%s,error=%s,ended_at=UTC_TIMESTAMP(6),lease_token=NULL,lease_until=NULL WHERE id=%s',
                        ('completed' if success else 'failed', None if success else (error or 'DSH execution failed')[:255], run_id))
            if success:
                # An older run can never replace a newer success (including a manual selection).
                cur.execute('SELECT current_run_id,display_revision FROM historian_reports WHERE id=%s FOR UPDATE', (row['report_id'],))
                display = cur.fetchone()
                prior = display['current_run_id']
                if prior:
                    cur.execute('SELECT created_at FROM historian_runs WHERE id=%s', (prior,))
                    newer = cur.fetchone()['created_at'] > row['created_at']
                else:
                    newer = False
                if not newer and display['display_revision']==row['display_revision']:
                    cur.execute('UPDATE historian_reports SET current_run_id=%s,display_revision=display_revision+1 WHERE id=%s', (run_id, row['report_id']))
            return {'ok': True}

    def phase(self, run_id, token, phase):
        if phase not in PHASES:
            raise ValueError('invalid investigation phase')
        with self.transaction() as cur:
            self.owned(cur, run_id, token)
            cur.execute('UPDATE historian_runs SET phase=%s WHERE id=%s', (phase, run_id))
        return {'ok': True}

    def account(self, run_id, token, record, recovery=False):
        with self.transaction() as cur:
            if recovery:
                cur.execute("SELECT * FROM historian_runs WHERE id=%s AND status IN ('failed','completed') FOR UPDATE", (run_id,))
                row = cur.fetchone()
                if not row:
                    raise Conflict('only terminal runs can reconcile native accounting')
            else:
                row = self.owned(cur, run_id, token)
            seq = record.get('native_event_seq')
            if not isinstance(seq, int) or seq < 0 or record.get('record_kind') not in {'attempt', 'task-end'}:
                raise ValueError('invalid native accounting event')
            session = row['dsh_session_id']
            if not str(record.get('task_key', '')).startswith(f'dsh:{session}:'):
                raise ValueError('accounting session mismatch')
            record_id = f'{session}:{seq}'
            record = {**record, 'report_run_id': run_id, 'report_id': row['report_id'],
                      'phase': 'context_compaction' if record.get('task_type')=='dsh_compaction' else 'recovered' if recovery else row['phase'], 'dsh_session_id': session}
            if record.get('supersedes_seq') is not None:
                record['supersedes_record_id'] = f"{session}:{record['supersedes_seq']}"
            cur.execute('INSERT IGNORE INTO yunying_ai_records(record_id,dsh_session_id,request_seq,payload) VALUES(%s,%s,%s,%s)',
                        (record_id, session, seq, encode(record)))
            cur.execute('INSERT IGNORE INTO historian_calls(record_id,run_id,phase,task_key) VALUES(%s,%s,%s,%s)',
                        (record_id, run_id, record['phase'], record['task_key']))
            return {'ok': True, 'record_id': record_id}

    def recovery(self, run_id=None):
        with self.transaction() as cur:
            if run_id:
                cur.execute("UPDATE historian_runs SET accounting_reconciled=TRUE WHERE id=%s AND status IN ('failed','completed')", (run_id,))
                return {'ok': True}
            cur.execute("SELECT r.id,r.dsh_session_id,p.kind,r.config FROM historian_runs r JOIN historian_reports p ON p.id=r.report_id "
                        "WHERE r.status IN ('failed','completed') AND r.accounting_reconciled=FALSE "
                        "AND r.ended_at<DATE_SUB(UTC_TIMESTAMP(6),INTERVAL 60 SECOND) "
                        f"AND p.server_id IN ({','.join(['%s']*len(self.config['servers']))}) ORDER BY r.created_at LIMIT 20",self.config['servers'])
            return {'ok': True, 'runs': [{**r, 'config': decode(r['config'])} for r in cur.fetchall()]}

    def select(self, report_id, run_id, expected):
        with self.transaction() as cur:
            cur.execute('SELECT current_run_id FROM historian_reports WHERE id=%s FOR UPDATE', (report_id,))
            row = cur.fetchone()
            if not row or row['current_run_id'] != expected:
                raise Conflict('display version changed; refresh before selecting')
            cur.execute("SELECT 1 FROM historian_runs WHERE id=%s AND report_id=%s AND status='completed'", (run_id, report_id))
            if not cur.fetchone():
                raise ValueError('version is not a completed run of this report')
            cur.execute('UPDATE historian_reports SET current_run_id=%s,display_revision=display_revision+1 WHERE id=%s', (run_id, report_id))
        return {'ok': True}

    def expire(self):
        with self.transaction() as cur:
            cur.execute("UPDATE historian_runs SET status='failed',error='worker lease expired',ended_at=UTC_TIMESTAMP(6),lease_token=NULL "
                        "WHERE status='running' AND lease_until<=UTC_TIMESTAMP(6)")

    def retry(self, run_id, actor, request_key):
        with self.transaction() as cur:
            cur.execute('SELECT p.* FROM historian_reports p JOIN historian_runs r ON r.report_id=p.id WHERE r.id=%s', (run_id,))
            row = cur.fetchone()
        if not row:
            raise ValueError('run not found')
        return self.create(row['server_id'], row['kind'], str(row['period_start']), row['timezone'], actor, request_key, run_id)
