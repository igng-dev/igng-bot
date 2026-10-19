"""Platform adapters: raw chat rows -> one normalized, privacy-reduced message shape.

Every platform adapter returns the same snapshot contract, so statistics, the DSH
worker and the site never read a platform's raw tables. Platform account IDs stay
in the server-side participant roster; the model only sees aliases and display names.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from .period import previous_days

PLATFORMS = {'qq'}
CONVERSATION_TYPES = {'group'}
EXTERNAL_ID = re.compile(r'[1-9][0-9]{0,18}')
TEXT_LIMIT = 4000
BOT_ALIAS = 'bot'


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def content_hash(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def iso(value):
    return instant(value).isoformat(timespec='milliseconds') + 'Z'


def conversation(platform, conversation_type, external_id):
    """Validate a conversation identity (same triple as yunying_sessions)."""
    external = str(external_id)
    if platform not in PLATFORMS or conversation_type not in CONVERSATION_TYPES or not EXTERNAL_ID.fullmatch(external):
        raise ValueError('unsupported chat conversation')
    return platform, conversation_type, external


# Account-, group- and phone-like numbers. Forwarded records and bare @mentions render IDs as text.
ACCOUNT_LIKE = re.compile(r'(?<![0-9A-Za-z])[1-9][0-9]{4,11}(?![0-9])')


def redact(text, names):
    """Known participants' account numbers become their display name; any other account-like number is masked."""
    return ACCOUNT_LIKE.sub(lambda m: names.get(m.group(0), '[号码]'), text)


def masked_name(account_id):
    tail = str(account_id)[-4:]
    return f'群友{tail}' if tail else '群友'


def _structure(value):
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


KIND_OF_SEGMENT = {'image': 'image', 'video': 'video', 'audio': 'voice', 'record': 'voice', 'file': 'file',
                   'forward': 'forward', 'node': 'forward', 'face': 'emoji', 'mface': 'sticker',
                   'card': 'card', 'markdown': 'text', 'xml': 'card', 'poke': 'interaction',
                   'dice': 'interaction', 'rps': 'interaction'}


def classify(structure, text):
    kinds = {KIND_OF_SEGMENT.get(part.get('type'), 'other') for part in structure
             if isinstance(part, dict) and part.get('type') not in {'text', 'at', 'reply'}}
    has_text = any(isinstance(p, dict) and p.get('type') == 'text' and str(p.get('text', '')).strip() for p in structure)
    if not structure:
        has_text = bool(str(text or '').strip())
    if has_text and kinds - {'emoji'}:
        return 'mixed'
    if has_text:
        return 'text'
    if len(kinds) == 1:
        return kinds.pop()
    return 'mixed' if kinds else 'other'


def emoji_tokens(structure):
    result = []
    for part in structure:
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'face' and part.get('id'):
            result.append('face:' + str(part['id'])[:16])
        elif part.get('type') == 'mface':
            result.append('sticker:' + str(part.get('summary') or part.get('emoji_id') or '?')[:40])
    return result


class Roster:
    """Stable per-snapshot aliases; the account IDs never leave the server."""

    def __init__(self, bot_account=None):
        self.bot_account = str(bot_account) if bot_account else None
        self.by_account, self.entries = {}, {}

    def alias(self, account, name=None, is_bot=False, spoke=False):
        account = str(account)
        is_bot = is_bot or (self.bot_account is not None and account == self.bot_account)
        alias = self.by_account.get(account)
        if alias is None:
            alias = BOT_ALIAS if is_bot else f'u{sum(1 for a in self.entries if a != BOT_ALIAS) + 1}'
            self.by_account[account] = alias
            self.entries[alias] = {'alias': alias, 'account': account, 'name': None, 'is_bot': is_bot, 'spoke': False,
                                   'name_source': None}
        entry = self.entries[alias]
        if name and not entry['name']:
            entry['name'], entry['name_source'] = str(name)[:120], 'snapshot'
        entry['spoke'] = entry['spoke'] or spoke
        return alias

    def finalize(self, fallback_names):
        for entry in self.entries.values():
            if entry['is_bot']:
                entry['name'], entry['name_source'] = entry['name'] or '云萤', entry['name_source'] or 'bot'
            elif not entry['name'] and fallback_names.get(entry['account']):
                entry['name'], entry['name_source'] = fallback_names[entry['account']], 'earlier_snapshot'
            elif not entry['name']:
                entry['name'], entry['name_source'] = masked_name(entry['account']), 'masked'
        return self.entries


def public_roster(entries):
    return [{'alias': e['alias'], 'name': e['name'], 'is_bot': e['is_bot'], 'spoke': e['spoke']} for e in entries.values()]


class QQGroupSource:
    """Reads the shared `message_logs` table for one QQ group (group_id > 0, never private)."""

    platform = 'qq'

    def __init__(self, connection_factory, bot_account=None, max_messages=60000):
        self.connect = connection_factory
        self.bot_account = bot_account
        self.max_messages = max_messages

    def known(self, external_id):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT 1 FROM group_configs WHERE group_id=%s UNION ALL '
                            '(SELECT 1 FROM message_logs WHERE group_id=%s LIMIT 1) LIMIT 1', (int(external_id), int(external_id)))
                return cur.fetchone() is not None
        finally:
            conn.close()

    def snapshot(self, external_id, label, zone, start, end):
        group = int(external_id)
        if group <= 0:
            raise ValueError('private conversations are never analysed')
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
                cur.execute('SELECT UTC_TIMESTAMP(6) AS cutoff')
                cutoff = cur.fetchone()['cutoff']
                cur.execute('SELECT MAX(id) AS last_id FROM message_logs WHERE group_id=%s', (group,))
                watermark = int(cur.fetchone()['last_id'] or 0)
                cur.execute('SELECT * FROM message_logs WHERE group_id=%s AND created_at>=%s AND created_at<%s AND id<=%s '
                            'ORDER BY created_at,id LIMIT %s', (group, start, end, watermark, self.max_messages + 1))
                rows = cur.fetchall()
                if len(rows) > self.max_messages:
                    raise ValueError('message budget exceeded for one day; raise CHAT_ANALYSIS_MAX_MESSAGES deliberately')
                live = [row for row in rows if not row.get('is_recalled')]
                senders = sorted({str(row['sender_id']) for row in rows if not row.get('is_self')})
                prior = set()
                if senders:
                    cur.execute('SELECT DISTINCT sender_id FROM message_logs WHERE group_id=%s AND created_at>=%s AND created_at<%s '
                                'AND is_self=0', (group, start - timedelta(days=90), start))
                    prior = {str(row['sender_id']) for row in cur.fetchall()}
                by_msg = {str(row['msg_id']): row for row in live}
                outside = sorted({str(row['reply_to_msg_id']) for row in live if row.get('reply_to_msg_id')
                                  and str(row['reply_to_msg_id']) not in by_msg})[:2000]
                earlier = {}
                if outside:
                    cur.execute('SELECT id,msg_id,sender_id,is_self,is_recalled FROM message_logs WHERE group_id=%s AND id<=%s '
                                f"AND msg_id IN ({','.join(['%s'] * len(outside))})", (group, watermark, *outside))
                    earlier = {str(row['msg_id']): row for row in cur.fetchall()}
                trend = []
                for day, lower, upper in previous_days(label, zone, 7):
                    cur.execute('SELECT COUNT(*) AS n, COUNT(DISTINCT sender_id) AS speakers FROM message_logs '
                                'WHERE group_id=%s AND created_at>=%s AND created_at<%s AND is_self=0 AND is_recalled=0',
                                (group, lower, upper))
                    found = cur.fetchone()
                    trend.append({'date': day, 'messages': int(found['n'] or 0), 'speakers': int(found['speakers'] or 0)})
                fallback = {}
                missing = sorted({str(row['sender_id']) for row in live if not row.get('sender_name')})[:2000]
                if missing and 'sender_name' in (rows[0] if rows else {}):
                    cur.execute('SELECT sender_id,sender_name FROM message_logs WHERE group_id=%s AND sender_name IS NOT NULL '
                                f"AND id<=%s AND sender_id IN ({','.join(['%s'] * len(missing))}) ORDER BY id DESC LIMIT 20000",
                                (group, watermark, *missing))
                    for row in cur.fetchall():
                        fallback.setdefault(str(row['sender_id']), row['sender_name'])
                conn.commit()
        finally:
            conn.close()
        return self.normalize(rows, earlier, prior, trend, fallback, cutoff, watermark)

    def normalize(self, rows, earlier, prior, trend, fallback, cutoff, watermark):
        roster = Roster(self.bot_account)
        live = [row for row in rows if not row.get('is_recalled')]
        by_msg = {str(row['msg_id']): row for row in live}
        messages = []
        for row in live:
            structure = _structure(row.get('message_structure'))
            is_bot = bool(row.get('is_self'))
            alias = roster.alias(row['sender_id'], row.get('sender_name'), is_bot, spoke=True)
            text = str(row.get('message_content') or row.get('plain_text_content') or '')
            if row.get('audio_transcript'):
                text = (text + ' [语音转写] ' + str(row['audio_transcript'])).strip()
            mentions = []
            for part in structure:
                if isinstance(part, dict) and part.get('type') == 'at' and str(part.get('qq', '')).isdigit():
                    mentions.append(roster.alias(part['qq'], part.get('name')))
                elif isinstance(part, dict) and part.get('type') == 'at' and part.get('qq') == 'all':
                    mentions.append('all')
            reply = None
            target = row.get('reply_to_msg_id')
            if target:
                found = by_msg.get(str(target))
                if found:
                    reply = {'ref': f"qq:{found['id']}", 'to': roster.alias(found['sender_id'], found.get('sender_name'),
                                                                           bool(found.get('is_self')))}
                elif str(target) in earlier and not earlier[str(target)].get('is_recalled'):
                    prior_row = earlier[str(target)]
                    reply = {'ref': None, 'to': roster.alias(prior_row['sender_id'], None, bool(prior_row.get('is_self'))),
                             'earlier': True}
                else:
                    reply = {'ref': None, 'to': None, 'earlier': True}
            messages.append({'ref': f"qq:{row['id']}", 'platform': 'qq',
                             'source': {'table': 'message_logs', 'id': str(row['id']), 'msg_id': str(row['msg_id'])},
                             'at': iso(row['created_at']), 'sender': alias, 'is_bot': is_bot,
                             'kind': classify(structure, text), 'text': text[:TEXT_LIMIT], 'truncated': len(text) > TEXT_LIMIT,
                             'reply_to': reply, 'mentions': sorted(set(mentions)), 'emoji': emoji_tokens(structure),
                             'time_precision': 'second'})
        entries = roster.finalize(fallback)
        # Second pass: the full roster is only known now. Nothing downstream (model, statistics, report) sees raw IDs.
        names = {e['account']: e['name'] for e in entries.values()}
        for message in messages:
            message['text'] = redact(message['text'], names)
        human = [m for m in messages if not m['is_bot']]
        speakers = {m['sender'] for m in human}
        context = {'recalled': sum(1 for row in rows if row.get('is_recalled')),
                   'new_speakers': sorted(a for a in speakers if entries[a]['account'] not in prior),
                   'trend': trend,
                   'name_sources': {a: e['name_source'] for a, e in entries.items()}}
        manifest = {'cutoff': str(cutoff), 'watermarks': {'message_logs': str(watermark)},
                    'message_count': len(messages), 'human_message_count': len(human),
                    'participants': list(entries.values()), 'time_precision': 'second; same-second order follows row id',
                    'excluded': {'recalled': context['recalled']}}
        return messages, context, manifest


def model_view(message, names):
    """Compact timeline line for the model; QQ numbers and source URLs are never included."""
    text = message['text']
    view = {'ref': message['ref'], 'at': message['at'], 'from': message['sender'],
            'name': names.get(message['sender']), 'kind': message['kind'], 'text': text[:500]}
    if len(text) > 500 or message.get('truncated'):
        view['text_truncated'] = True
    if message['is_bot']:
        view['is_bot'] = True
    if message.get('reply_to'):
        view['reply_to'] = {k: v for k, v in message['reply_to'].items() if v is not None}
    if message.get('mentions'):
        view['mentions'] = message['mentions']
    return view
