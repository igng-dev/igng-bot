"""Read raw MC facts, never infer a narrative or expose arbitrary SQL to DSH."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def normalize(table, row):
    at = instant(row['sent_at'] if table == 'chat_messages' else row['occurred_at'])
    # No IP addresses, login secrets or inferred account links enter the model.
    result = {'event_id': f"mc:{row['server_id']}:{table}:{row['id']}",
              'source': {'table': table, 'id': str(row['id']), 'server_id': int(row['server_id'])},
              'occurred_at': at.isoformat(timespec='milliseconds') + 'Z',
              'type': 'CHAT' if table == 'chat_messages' else row['event_type'],
              'player_uuid': row.get('player_uuid'), 'player_name': row.get('player_name'),
              'boot_id': row.get('boot_id'), 'capture_seq': row.get('capture_seq'),
              'is_fake': None if row.get('is_fake') is None else bool(row['is_fake']),
              'time_precision': 'millisecond' if row.get('capture_seq') is not None else 'legacy_second'}
    fields = ('content', 'original_content', 'cancelled', 'source') if table == 'chat_messages' else (
        'related_uuid', 'related_name', 'related_entity_type', 'related_is_fake', 'direct_entity_type',
        'cause', 'damage_type', 'world', 'x', 'y', 'z', 'reason')
    result['facts'] = {k: row[k] for k in fields if k in row and row[k] is not None}
    if 'related_is_fake' in result['facts']:
        result['facts']['related_is_fake'] = bool(result['facts']['related_is_fake'])
    return result


def ordered(events):
    # Capture sequence resolves ties only within a known plugin boot, not across machines.
    return sorted(events, key=lambda e: (e['occurred_at'], e.get('boot_id') or '',
                                        int(e.get('capture_seq') or 0), e['source']['table'], int(e['source']['id'])))


class MCSource:
    def __init__(self, connection_factory, max_events=500000, tables=None):
        self.connect = connection_factory
        self.max_events = max_events
        self.tables = tables or {}

    def table(self, name):
        database = self.tables.get(name)
        if database and not re.fullmatch(r'[A-Za-z0-9_$]+', database):
            raise ValueError('invalid MC schema name')
        return f'`{database}`.`{name}`' if database else f'`{name}`'

    def snapshot(self, server_id, start, end):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
                cur.execute('SELECT UTC_TIMESTAMP(6) AS cutoff')
                cutoff = cur.fetchone()['cutoff']
                events, watermarks = [], {}
                # Look back for midnight carry-in and retention; never send that statistical history to AI.
                history_start = start - timedelta(days=90)
                history = []
                for table, column in [('chat_messages', 'sent_at'), ('player_event_logs', 'occurred_at')]:
                    sql_table = self.table(table)
                    cur.execute(f'SELECT MAX(id) AS last_id FROM {sql_table} WHERE server_id=%s', (server_id,))
                    watermarks[table] = str(cur.fetchone()['last_id'] or 0)
                    cur.execute(f'SELECT * FROM {sql_table} WHERE server_id=%s AND {column}>=%s AND {column}<%s '
                                f'AND id<=%s ORDER BY {column},id LIMIT %s',
                                (server_id,start,end,watermarks[table],self.max_events + 1))
                    rows = cur.fetchall()
                    if len(rows) > self.max_events:
                        raise ValueError('event budget exceeded; narrow the configured scope')
                    for row in rows:
                        event = normalize(table, row)
                        if table == 'player_event_logs':
                            history.append(event)
                        if instant(event['occurred_at']) >= start:
                            events.append(event)
                    if table == 'player_event_logs':
                        # Two indexable carry-in queries, not a scan of 90 days of positions.
                        for lower, predicate in [(history_start,"event_type IN ('JOIN','QUIT','KICK','RECORDER_START','RECORDER_STOP','SERVER_START','SERVER_STOP')"),
                                                 (start-timedelta(days=1),"event_type='PRESENCE'")]:
                            cur.execute(f'SELECT * FROM {sql_table} WHERE server_id=%s AND {predicate} AND occurred_at>=%s AND occurred_at<%s '
                                        'AND id<=%s ORDER BY occurred_at,id LIMIT %s',
                                        (server_id,lower,start,watermarks[table],self.max_events+1))
                            history.extend(normalize(table,row) for row in cur.fetchall())
                            if len(history)>self.max_events:
                                raise ValueError('statistical history budget exceeded')
                if len(events) > self.max_events:
                    raise ValueError('event budget exceeded')
                conn.commit()
                return ordered(events), ordered(history), {'cutoff': str(cutoff), 'watermarks': watermarks,
                    'history_from': str(history_start), 'historical_order': 'same-second order is unknown without capture_seq',
                    'event_count': len(events)}
        finally:
            conn.close()


def content_hash(event):
    return hashlib.sha256(encode(event).encode()).hexdigest()
