"""Validate the model's structured report and render it; quotes are copied by the server, never typed by the model.

Report v2 is built for a reader who did not see the chat: a few plain highlights, then each topic as one-sentence
gist + chronological points + outcome. The prose lint rejects what readers would otherwise have to "translate"
(aliases, refs, certainty codes, empty phrases, hedge stacking) so the model repairs it before publication.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from .source import BOT_ALIAS

VERSION = 2
CERTAINTY = {'fact', 'inference', 'speculation'}
CERTAINTY_LABEL = {'fact': '', 'inference': '（推断）', 'speculation': '（推测）'}
OPEN_KINDS = {'question': '待回答的问题', 'plan': '约定与计划', 'announcement': '公告与通知', 'request': '求助与请求'}
OUTCOMES = {'settled': '有结论', 'open': '还没结论', 'none': '闲聊，无需结论'}
ALIAS = re.compile(r'(?:u[1-9][0-9]{0,4}|bot)')
# Forwarded chat records and cards are other people's words, not the member's own.
UNQUOTABLE = {'forward', 'card'}
SECTIONS = ('highlights', 'topics', 'quotes', 'titles', 'open_items', 'slang')

# Prose lint: things a member reading the report should never have to decode.
ALIAS_IN_TEXT = re.compile(r'(?<![A-Za-z0-9_])u[1-9][0-9]{0,4}(?![A-Za-z0-9_])')
REF_IN_TEXT = re.compile(r'qq:[0-9]+')
CODE_IN_TEXT = re.compile(r'(?<![A-Za-z])(?:fact|inference|speculation|evidence_refs|summary_refs)(?![A-Za-z])', re.I)
EMPTY_PHRASES = ('热烈讨论', '展开了讨论', '展开讨论', '引发热议', '引发了热议', '气氛融洽', '氛围融洽', '其乐融融', '欢声笑语',
                 '精彩纷呈', '不亦乐乎', '热闹非凡', '充满活力', '温馨和谐', '碰撞出', '掀起了', '体现了', '彰显')
HEDGES = re.compile(r'可能|似乎|或许|也许|大概|好像')


class Invalid(ValueError):
    pass


def _text(value, field, low, high):
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise Invalid(f'{field} must be text of {low}..{high} characters')
    return value.strip()


def _list(value, field, low, high):
    if value is None and low == 0:
        return []
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise Invalid(f'{field} must be a list of {low}..{high} items')
    return value


def lint(text, field, names=()):
    """Reject prose a reader would have to translate; the message says exactly what to rewrite."""
    for match in ALIAS_IN_TEXT.findall(text):
        if not any(match in name for name in names):
            raise Invalid(f'{field}: write the display name, not the alias {match}')
    if REF_IN_TEXT.search(text):
        raise Invalid(f'{field}: message refs belong in evidence_refs, not in the text')
    code = CODE_IN_TEXT.search(text)
    if code:
        raise Invalid(f'{field}: do not write the field/certainty code "{code.group(0)}" in the text')
    for phrase in EMPTY_PHRASES:
        if phrase in text:
            raise Invalid(f'{field}: "{phrase}" says nothing concrete; write who said or did what instead')
    if len(HEDGES.findall(text)) > 1:
        raise Invalid(f'{field}: at most one hedge word (可能/似乎…) per item; if the cause is unknown, say 记录里看不出原因')
    return text


class Validator:
    def __init__(self, read_refs, roster, messages, human_messages):
        self.read_refs, self.roster, self.messages = set(read_refs), roster, messages
        self.human_messages = human_messages
        self.names = tuple(str(p.get('name') or '') for p in roster.values())
        self.cited = set()

    def prose(self, value, field, low, high):
        return lint(_text(value, field, low, high), field, self.names)

    def refs(self, value, field, high=40):
        refs = _list(value, field, 1, high)
        if any(not isinstance(ref, str) or not 1 <= len(ref) <= 64 for ref in refs) or len(set(refs)) != len(refs):
            raise Invalid(f'{field} requires distinct message refs')
        missing = [ref for ref in refs if ref not in self.read_refs]
        if missing:
            raise Invalid(f'{field}: refs must be real messages of this day that you have read: {", ".join(missing[:5])}')
        self.cited.update(refs)
        return refs

    def span(self, refs):
        times = sorted(self.messages[ref]['at'] for ref in refs if ref in self.messages and self.messages[ref].get('at'))
        return {'from': times[0], 'to': times[-1]} if times else None

    def certainty(self, value, field):
        if value not in CERTAINTY:
            raise Invalid(f'{field}.certainty must be fact, inference or speculation')
        return value

    def member(self, value, field, human=True):
        if not isinstance(value, str) or not ALIAS.fullmatch(value) or value not in self.roster:
            raise Invalid(f'{field} must be a participant alias such as u1')
        if human and (value == BOT_ALIAS or self.roster[value].get('is_bot')):
            raise Invalid(f'{field} cannot be the bot')
        return value

    def highlight(self, item, n):
        field = f'highlights[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        return {'text': self.prose(item.get('text'), field + '.text', 1, 60),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs', 10)}

    def topic(self, item, n):
        field = f'topics[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        participants = _list(item.get('participants'), field + '.participants', 1, 30)
        points = []
        for m, point in enumerate(_list(item.get('points'), field + '.points', 1, 6)):
            if not isinstance(point, dict):
                raise Invalid(f'{field}.points[{m}] must be an object')
            points.append({'text': self.prose(point.get('text'), f'{field}.points[{m}].text', 1, 60),
                           'evidence_refs': self.refs(point.get('evidence_refs'), f'{field}.points[{m}].evidence_refs', 10)})
        outcome = item.get('outcome')
        if not isinstance(outcome, dict) or outcome.get('status') not in OUTCOMES:
            raise Invalid(field + '.outcome.status must be one of ' + ', '.join(OUTCOMES))
        outcome_text = outcome.get('text') or ''
        if outcome['status'] != 'none' or outcome_text:
            outcome_text = self.prose(outcome_text, field + '.outcome.text', 1, 40)
        gist = self.prose(item.get('gist'), field + '.gist', 1, 80)
        evidence = list(dict.fromkeys(ref for point in points for ref in point['evidence_refs']))
        # v1 readers (site, previous-day leads) only know topic.summary; derive it instead of asking the model twice.
        summary = gist + ' ' + '；'.join(p['text'] for p in points) + (f'。结果：{outcome_text}' if outcome_text else '')
        return {'title': self.prose(item.get('title'), field + '.title', 1, 20), 'gist': gist, 'points': points,
                'outcome': {'status': outcome['status'], 'text': outcome_text}, 'summary': summary,
                'participants': sorted({self.member(a, field + '.participants', human=False) for a in participants},
                                       key=participants.index),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': evidence, 'time_range': self.span(evidence)}

    def quote(self, item, n):
        field = f'quotes[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        ref = self.refs([item.get('ref')], field + '.ref', 1)[0]
        message = self.messages.get(ref)
        if not message or message['is_bot']:
            raise Invalid(field + ' must quote a member message, not the bot')
        if message['kind'] in UNQUOTABLE:
            raise Invalid(field + ' must be something the member typed, not a forwarded record or card')
        if not message['text'].strip():
            raise Invalid(field + ' has no quotable text')
        context = item.get('context') or ''
        if not isinstance(context, str) or len(context) > 80:
            raise Invalid(field + '.context must be at most 80 characters')
        context = lint(context.strip(), field + '.context', self.names)
        # The verbatim text and speaker are taken from the snapshot, not from the model. `comment` keeps v1 readers working.
        return {'ref': ref, 'source_id': message['source']['id'], 'speaker': message['sender'], 'at': message['at'],
                'text': message['text'][:300], 'context': context, 'comment': context}

    def title(self, item, n, seen):
        field = f'titles[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        member = self.member(item.get('member'), field + '.member')
        if member in seen:
            raise Invalid(field + ': one title per member')
        seen.add(member)
        refs = self.refs(item.get('evidence_refs'), field + '.evidence_refs', 10)
        if not any(self.messages.get(ref, {}).get('sender') == member for ref in refs):
            raise Invalid(field + ': evidence must include at least one message by that member')
        return {'member': member, 'title': self.prose(item.get('title'), field + '.title', 1, 16),
                'reason': self.prose(item.get('reason'), field + '.reason', 1, 60), 'evidence_refs': refs}

    def open_item(self, item, n):
        field = f'open_items[{n}]'
        if not isinstance(item, dict) or item.get('kind') not in OPEN_KINDS:
            raise Invalid(field + '.kind must be one of ' + ', '.join(OPEN_KINDS))
        return {'kind': item['kind'], 'summary': self.prose(item.get('summary'), field + '.summary', 1, 80),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs', 10)}

    def slang(self, item, n):
        field = f'slang[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        return {'term': _text(item.get('term'), field + '.term', 1, 30),
                'meaning': self.prose(item.get('meaning'), field + '.meaning', 1, 80),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs', 10)}

    def validate(self, data, partial=False):
        if not isinstance(data, dict):
            raise Invalid('report must be an object')
        unknown = set(data) - {'headline', *SECTIONS}
        if unknown:
            raise Invalid('unknown report fields: ' + ', '.join(sorted(unknown)))
        result = {}
        if not partial or 'headline' in data:
            result['headline'] = self.prose(data.get('headline'), 'headline', 1, 50)
        if not partial or 'highlights' in data:
            highlights = [self.highlight(h, n) for n, h in
                          enumerate(_list(data.get('highlights'), 'highlights', 0 if partial else 1, 5))]
            result['highlights'] = highlights
            # v1 fields, derived so the current site and previous-day leads keep rendering.
            result['summary'] = '；'.join(h['text'] for h in highlights)
            result['summary_refs'] = list(dict.fromkeys(ref for h in highlights for ref in h['evidence_refs']))
        if not partial or 'topics' in data:
            result['topics'] = [self.topic(t, n) for n, t in enumerate(_list(data.get('topics'), 'topics', 0 if partial else 1, 10))]
        if 'quotes' in data or not partial:
            quotes = [self.quote(q, n) for n, q in enumerate(_list(data.get('quotes'), 'quotes', 0, 8))]
            if len({q['ref'] for q in quotes}) != len(quotes):
                raise Invalid('quotes must be distinct messages')
            result['quotes'] = quotes
        if 'titles' in data or not partial:
            seen = set()
            result['titles'] = [self.title(t, n, seen) for n, t in enumerate(_list(data.get('titles'), 'titles', 0, 10))]
        if 'open_items' in data or not partial:
            result['open_items'] = [self.open_item(o, n) for n, o in enumerate(_list(data.get('open_items'), 'open_items', 0, 10))]
        if 'slang' in data or not partial:
            result['slang'] = [self.slang(s, n) for n, s in enumerate(_list(data.get('slang'), 'slang', 0, 8))]
        if not partial:
            result['version'] = VERSION
            result['relations'] = []  # v2 dropped relations; the site still reads the key.
        return result


def clock(value, zone):
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return moment.astimezone(ZoneInfo(zone)).strftime('%H:%M') if zone else moment.strftime('%H:%M')
    except (ValueError, KeyError):
        return ''


def render_markdown(result, names, header, zone=None):
    def name(alias):
        return names.get(alias, alias)

    def qualified(text, certainty):
        return text + CERTAINTY_LABEL.get(certainty, '')

    lines = [f'# {result["headline"]}', '', header]
    if result.get('highlights'):
        lines += ['', '## 今天最值得知道的', '']
        lines += [f'- {h["text"]}' for h in result['highlights']]
    if result.get('topics'):
        lines += ['', '## 今日话题']
        for n, topic in enumerate(result['topics'], 1):
            span = topic.get('time_range')
            when = f'（{clock(span["from"], zone)}–{clock(span["to"], zone)}）' if span else ''
            lines += ['', f'### {n}. {topic["title"]}{when}', '', qualified(topic['gist'], topic['certainty']), '']
            lines += [f'- {p["text"]}' for p in topic['points']]
            outcome = topic['outcome']
            lines += ['', f'结果：{OUTCOMES[outcome["status"]]}' + (f'，{outcome["text"]}' if outcome['text'] else ''),
                      f'参与：{"、".join(name(a) for a in topic["participants"])}']
    if result.get('quotes'):
        lines += ['', '## 今日金句']
        for quote in result['quotes']:
            text = quote['text'].replace('\n', ' ')
            lines += ['', f'> {text}', f'> —— {name(quote["speaker"])} {clock(quote["at"], zone)}'.rstrip()
                      + (f'（{quote["context"]}）' if quote['context'] else '')]
    if result.get('titles'):
        lines += ['', '## 今日称号', '']
        lines += [f'- **{name(t["member"])}** · {t["title"]}：{t["reason"]}' for t in result['titles']]
    if result.get('open_items'):
        lines += ['', '## 待跟进', '']
        lines += [f'- 【{OPEN_KINDS[o["kind"]]}】' + qualified(o['summary'], o['certainty']) for o in result['open_items']]
    if result.get('slang'):
        lines += ['', '## 新梗观察', '']
        lines += [f'- **{s["term"]}**：' + qualified(s['meaning'], s['certainty']) for s in result['slang']]
    return '\n'.join(lines).strip() + '\n'


def render_empty(header):
    return f'# 今天群里很安静\n\n{header}\n\n当天的可用记录里没有群友发言（撤回的消息不计入）。这不代表群不存在其他形式的活动。\n'
