"""Validate the model's structured report and render it; quotes are copied by the server, never typed by the model."""
import re
from .source import BOT_ALIAS

CERTAINTY = {'fact', 'inference', 'speculation'}
CERTAINTY_LABEL = {'fact': '', 'inference': '（推断）', 'speculation': '（推测）'}
OPEN_KINDS = {'question': '待回答的问题', 'plan': '约定与计划', 'announcement': '公告与通知', 'request': '求助与请求'}
ALIAS = re.compile(r'(?:u[1-9][0-9]{0,4}|bot)')
SECTIONS = ('mood', 'topics', 'quotes', 'titles', 'relations', 'open_items', 'slang')


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


class Validator:
    def __init__(self, read_refs, roster, messages, human_messages):
        self.read_refs, self.roster, self.messages = set(read_refs), roster, messages
        self.human_messages = human_messages
        self.cited = set()

    def refs(self, value, field, high=40):
        refs = _list(value, field, 1, high)
        if any(not isinstance(ref, str) or not 1 <= len(ref) <= 64 for ref in refs) or len(set(refs)) != len(refs):
            raise Invalid(f'{field} requires distinct message refs')
        missing = [ref for ref in refs if ref not in self.read_refs]
        if missing:
            raise Invalid(f'{field}: refs must be real messages of this day that you have read: {", ".join(missing[:5])}')
        self.cited.update(refs)
        return refs

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

    def mood(self, item):
        if not isinstance(item, dict):
            raise Invalid('mood must be an object')
        return {'label': _text(item.get('label'), 'mood.label', 1, 20),
                'description': _text(item.get('description'), 'mood.description', 1, 600),
                'certainty': self.certainty(item.get('certainty'), 'mood'),
                'evidence_refs': self.refs(item.get('evidence_refs'), 'mood.evidence_refs', 20)}

    def topic(self, item, n):
        field = f'topics[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        participants = _list(item.get('participants'), field + '.participants', 1, 30)
        return {'title': _text(item.get('title'), field + '.title', 1, 60),
                'summary': _text(item.get('summary'), field + '.summary', 1, 1500),
                'participants': sorted({self.member(a, field + '.participants', human=False) for a in participants},
                                       key=participants.index),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs')}

    def quote(self, item, n):
        field = f'quotes[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        ref = self.refs([item.get('ref')], field + '.ref', 1)[0]
        message = self.messages.get(ref)
        if not message or message['is_bot']:
            raise Invalid(field + ' must quote a member message, not the bot')
        if not message['text'].strip():
            raise Invalid(field + ' has no quotable text')
        comment = item.get('comment') or ''
        if not isinstance(comment, str) or len(comment) > 200:
            raise Invalid(field + '.comment must be at most 200 characters')
        # The verbatim text and speaker are taken from the snapshot, not from the model.
        return {'ref': ref, 'source_id': message['source']['id'], 'speaker': message['sender'], 'at': message['at'],
                'text': message['text'][:300], 'comment': comment.strip()}

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
        return {'member': member, 'title': _text(item.get('title'), field + '.title', 1, 16),
                'reason': _text(item.get('reason'), field + '.reason', 1, 200), 'evidence_refs': refs}

    def relation(self, item, n):
        field = f'relations[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        members = _list(item.get('members'), field + '.members', 2, 6)
        members = [self.member(a, field + '.members', human=False) for a in members]
        if len(set(members)) != len(members):
            raise Invalid(field + '.members must be distinct')
        return {'members': members, 'summary': _text(item.get('summary'), field + '.summary', 1, 500),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs', 20)}

    def open_item(self, item, n):
        field = f'open_items[{n}]'
        if not isinstance(item, dict) or item.get('kind') not in OPEN_KINDS:
            raise Invalid(field + '.kind must be one of ' + ', '.join(OPEN_KINDS))
        return {'kind': item['kind'], 'summary': _text(item.get('summary'), field + '.summary', 1, 400),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs', 10)}

    def slang(self, item, n):
        field = f'slang[{n}]'
        if not isinstance(item, dict):
            raise Invalid(field + ' must be an object')
        return {'term': _text(item.get('term'), field + '.term', 1, 30),
                'meaning': _text(item.get('meaning'), field + '.meaning', 1, 200),
                'certainty': self.certainty(item.get('certainty'), field),
                'evidence_refs': self.refs(item.get('evidence_refs'), field + '.evidence_refs', 10)}

    def validate(self, data, partial=False):
        if not isinstance(data, dict):
            raise Invalid('report must be an object')
        unknown = set(data) - {'headline', 'summary', 'summary_refs', *SECTIONS}
        if unknown:
            raise Invalid('unknown report fields: ' + ', '.join(sorted(unknown)))
        result = {}
        if not partial or 'headline' in data:
            result['headline'] = _text(data.get('headline'), 'headline', 1, 80)
        if not partial or 'summary' in data:
            result['summary'] = _text(data.get('summary'), 'summary', 1, 1500)
            result['summary_refs'] = self.refs(data.get('summary_refs'), 'summary_refs', 20)
        if not partial or 'mood' in data:
            result['mood'] = self.mood(data.get('mood'))
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
        if 'relations' in data or not partial:
            result['relations'] = [self.relation(r, n) for n, r in enumerate(_list(data.get('relations'), 'relations', 0, 8))]
        if 'open_items' in data or not partial:
            result['open_items'] = [self.open_item(o, n) for n, o in enumerate(_list(data.get('open_items'), 'open_items', 0, 10))]
        if 'slang' in data or not partial:
            result['slang'] = [self.slang(s, n) for n, s in enumerate(_list(data.get('slang'), 'slang', 0, 8))]
        return result


def render_markdown(result, names, header):
    def name(alias):
        return names.get(alias, alias)

    def qualified(text, certainty):
        return text + CERTAINTY_LABEL.get(certainty, '')

    lines = [f'# {result["headline"]}', '', header, '', result['summary']]
    mood = result.get('mood')
    if mood:
        lines += ['', f'**群氛围：{mood["label"]}** — ' + qualified(mood['description'], mood['certainty'])]
    if result.get('topics'):
        lines += ['', '## 今日话题']
        for n, topic in enumerate(result['topics'], 1):
            who = '、'.join(name(a) for a in topic['participants'])
            lines += ['', f'### {n}. {topic["title"]}', '', qualified(topic['summary'], topic['certainty']), '', f'参与：{who}']
    if result.get('quotes'):
        lines += ['', '## 今日金句']
        for quote in result['quotes']:
            text = quote['text'].replace('\n', ' ')
            lines += ['', f'> {text}', f'> —— {name(quote["speaker"])}' + (f'｜{quote["comment"]}' if quote['comment'] else '')]
    if result.get('titles'):
        lines += ['', '## 今日称号', '']
        lines += [f'- **{name(t["member"])}** · {t["title"]}：{t["reason"]}' for t in result['titles']]
    if result.get('relations'):
        lines += ['', '## 互动关系', '']
        lines += [f'- {" × ".join(name(a) for a in r["members"])}：' + qualified(r['summary'], r['certainty']) for r in result['relations']]
    if result.get('open_items'):
        lines += ['', '## 待跟进', '']
        lines += [f'- 【{OPEN_KINDS[o["kind"]]}】' + qualified(o['summary'], o['certainty']) for o in result['open_items']]
    if result.get('slang'):
        lines += ['', '## 新梗观察', '']
        lines += [f'- **{s["term"]}**：' + qualified(s['meaning'], s['certainty']) for s in result['slang']]
    return '\n'.join(lines).strip() + '\n'


def render_empty(header):
    return f'# 今天群里很安静\n\n{header}\n\n当天的可用记录里没有群友发言（撤回的消息不计入）。这不代表群不存在其他形式的活动。\n'
