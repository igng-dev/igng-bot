"""Offline chat analysis: adapter normalization, statistics, evidence validation and scheduling."""
import asyncio
import json
from datetime import datetime, timezone
import pytest
from igngbot_shared.db import normalize_sender_name
from igngbot_v4.chat_analysis.period import completed_days, previous_days, day_period
from igngbot_v4.chat_analysis.result import Validator, Invalid, render_markdown, render_empty
from igngbot_v4.chat_analysis.service import Service, settings
from igngbot_v4.chat_analysis.source import QQGroupSource, conversation, model_view, public_roster
from igngbot_v4.chat_analysis.statistics import compute

BOT = 3000000001


def row(id, sender, text, minute, **extra):
    base = {'id': id, 'group_id': 123456, 'sender_id': sender, 'sender_name': None, 'message_content': text,
            'plain_text_content': text, 'message_structure': json.dumps([{'type': 'text', 'text': text}], ensure_ascii=False),
            'reply_to_msg_id': None, 'msg_id': f'm{id}', 'audio_transcript': None, 'is_self': 0, 'is_recalled': 0,
            'created_at': datetime(2026, 10, 6, 16, 0) .replace(minute=minute)}
    base.update(extra)
    return base


def snapshot():
    rows = [
        row(1, 10001, '今晚谁一起开黑', 0, sender_name='阿青'),
        row(2, 10002, '我来！', 1, sender_name='小白', reply_to_msg_id='m1'),
        row(3, 10003, '撤回的秘密', 2, is_recalled=1),
        row(4, 10002, '@云萤 你也来', 3, sender_name='小白',
            message_structure=json.dumps([{'type': 'at', 'qq': str(BOT), 'name': '云萤'}, {'type': 'text', 'text': ' 你也来'}])),
        row(5, BOT, '我只会看着你们玩', 4, is_self=1),
        row(6, 10004, '[图片]', 5, message_structure=json.dumps([{'type': 'image', 'file': 'a.jpg', 'url': 'http://x/a.jpg'}])),
        row(7, 10001, '', 6, sender_name='阿青', audio_transcript='那就九点见', message_content='[语音]',
            message_structure=json.dumps([{'type': 'record', 'file': 'v.amr'}]), reply_to_msg_id='old-1'),
    ]
    earlier = {'old-1': {'id': 0, 'msg_id': 'old-1', 'sender_id': 10002, 'is_self': 0, 'is_recalled': 0}}
    trend = [{'date': f'2026-09-{29 + i}' if i < 2 else f'2026-10-0{i - 1}', 'messages': 4, 'speakers': 2} for i in range(7)]
    source = QQGroupSource(None, BOT)
    return source.normalize(rows, earlier, {'10001'}, trend, {'10004': '旧名'}, '2026-10-08 00:00:00', 7)


def test_qq_adapter_aliases_redact_accounts_and_drop_recalled_messages():
    messages, context, manifest = snapshot()
    assert [m['ref'] for m in messages] == ['qq:1', 'qq:2', 'qq:4', 'qq:5', 'qq:6', 'qq:7']
    assert context['recalled'] == 1 and manifest['human_message_count'] == 5
    roster = {p['alias']: p for p in manifest['participants']}
    assert roster['u1']['name'] == '阿青' and roster['bot']['is_bot'] and roster['bot']['name'] == '云萤'
    assert roster['u3']['name'] == '旧名' and roster['u3']['name_source'] == 'earlier_snapshot'
    assert messages[1]['reply_to'] == {'ref': 'qq:1', 'to': 'u1'}
    assert messages[2]['mentions'] == ['bot']
    assert messages[4]['kind'] == 'image'
    assert '那就九点见' in messages[5]['text'] and messages[5]['reply_to']['earlier'] is True and messages[5]['reply_to']['to'] == 'u2'
    assert context['new_speakers'] == ['u2', 'u3']
    names = {p['alias']: p['name'] for p in manifest['participants']}
    model_text = json.dumps([model_view(m, names) for m in messages] + public_roster(roster), ensure_ascii=False)
    for secret in ('10001', '10002', '10004', str(BOT), '撤回的秘密', 'http://x/a.jpg'):
        assert secret not in model_text


def test_masked_name_when_no_snapshot_exists():
    source = QQGroupSource(None, BOT)
    messages, _, manifest = source.normalize([row(1, 55556789, '在吗', 0)], {}, set(), [], {}, 'now', 1)
    assert manifest['participants'][0]['name'] == '群友6789'
    assert normalize_sender_name('55556789', 55556789) is None
    assert normalize_sender_name('  阿\n青 ', 1) == '阿 青'


def test_statistics_are_local_time_and_capture_interactions():
    messages, context, manifest = snapshot()
    start, end = day_period('2026-10-07', 'Asia/Shanghai', datetime(2026, 10, 9, tzinfo=timezone.utc))
    stats = compute(messages, context, manifest, start, end, 'Asia/Shanghai')
    assert stats['messages'] == 5 and stats['speakers'] == 3 and stats['bot_messages'] == 1 and stats['recalled'] == 1
    assert stats['hourly'][0]['messages'] == 5 and stats['peak_hour'] == 0
    assert {'from': 'u2', 'to': 'u1', 'count': 1} in stats['reply_edges']
    assert stats['bot_addressed'] == 1
    assert stats['busiest_window']['messages'] == 5
    assert stats['previous_7d_average'] == 4.0 and stats['change_vs_average'] == 0.25
    assert stats['top_members'][0]['name'] in {'阿青', '小白'}
    assert 'recalled_messages_excluded' in stats['quality']['warnings']
    assert '10001' not in json.dumps(stats)


def validator(read=None):
    messages, _, manifest = snapshot()
    by_ref = {m['ref']: m for m in messages}
    roster = {p['alias']: p for p in manifest['participants']}
    return Validator(set(by_ref) if read is None else read, roster, by_ref, manifest['human_message_count'])


def report(**overrides):
    data = {'headline': '主要在约今晚九点开黑',
            'highlights': [{'text': '阿青约今晚开黑，小白马上答应', 'evidence_refs': ['qq:1', 'qq:2']}],
            'topics': [{'title': '今晚开黑', 'gist': '阿青喊人开黑，小白答应并叫上云萤，最后约在九点。',
                        'points': [{'text': '阿青问今晚谁一起开黑', 'evidence_refs': ['qq:1']},
                                   {'text': '阿青用语音说九点见', 'evidence_refs': ['qq:7']}],
                        'outcome': {'status': 'settled', 'text': '九点开黑'},
                        'participants': ['u1', 'u2', 'bot'], 'certainty': 'inference'}],
            'quotes': [{'ref': 'qq:2', 'context': '小白在回应阿青问谁一起开黑'}],
            'titles': [{'member': 'u1', 'title': '组局发起人', 'reason': '第一个喊人开黑', 'evidence_refs': ['qq:1']}],
            'open_items': [{'kind': 'plan', 'summary': '阿青和小白九点开黑', 'certainty': 'fact', 'evidence_refs': ['qq:7']}],
            'slang': []}
    data.update(overrides)
    return data


def topic(**overrides):
    return {**report()['topics'][0], **overrides}


def test_validator_accepts_evidence_and_copies_quotes_from_snapshot():
    result = validator().validate(report(quotes=[{'ref': 'qq:2', 'context': '小白在回应阿青', 'text': '伪造原话'}]))
    assert result['quotes'][0]['text'] == '我来！' and result['quotes'][0]['speaker'] == 'u2'
    markdown = render_markdown(result, {'u1': '阿青', 'u2': '小白', 'bot': '云萤'}, '2026-10-07 · Asia/Shanghai', 'Asia/Shanghai')
    assert '阿青' in markdown and '组局发起人' in markdown and '（推断）' in markdown and 'u1' not in markdown
    assert '伪造原话' not in markdown and 'qq:' not in markdown
    assert '### 1. 今晚开黑（00:00–00:06）' in markdown and '- 阿青问今晚谁一起开黑' in markdown
    assert '结果：有结论，九点开黑' in markdown and '（小白在回应阿青）' in markdown


def test_v2_report_keeps_v1_fields_for_existing_readers():
    result = validator().validate(report())
    assert result['version'] == 2 and result['relations'] == [] and 'mood' not in result
    assert result['summary'] == '阿青约今晚开黑，小白马上答应' and result['summary_refs'] == ['qq:1', 'qq:2']
    first = result['topics'][0]
    assert first['evidence_refs'] == ['qq:1', 'qq:7'] and first['summary'].startswith(first['gist'])
    assert first['time_range'] == {'from': '2026-10-06T16:00:00.000Z', 'to': '2026-10-06T16:06:00.000Z'}
    assert result['quotes'][0]['comment'] == result['quotes'][0]['context']


@pytest.mark.parametrize('overrides,message', [
    ({'highlights': [{'text': '阿青约人', 'evidence_refs': ['qq:999']}]}, 'refs must be real'),
    ({'titles': [{'member': 'bot', 'title': '机器人', 'reason': 'x', 'evidence_refs': ['qq:5']}]}, 'cannot be the bot'),
    ({'titles': [{'member': 'u2', 'title': '组局', 'reason': 'x', 'evidence_refs': ['qq:1']}]}, 'by that member'),
    ({'quotes': [{'ref': 'qq:5'}]}, 'not the bot'),
    ({'topics': []}, 'topics must be a list of 1..10'),
    ({'highlights': []}, 'highlights must be a list of 1..5'),
    ({'open_items': [{'kind': 'plan', 'summary': '九点', 'certainty': 'sure', 'evidence_refs': ['qq:7']}]}, 'certainty'),
    ({'extra': 1}, 'unknown report fields'),
    ({'mood': {'label': '热络'}}, 'unknown report fields'),
    ({'titles': [{'member': 'u1', 'title': '一' * 17, 'reason': 'x', 'evidence_refs': ['qq:1']}]}, 'title must be text'),
    ({'topics': [topic(points=[])]}, 'points must be a list of 1..6'),
    ({'topics': [topic(outcome={'status': 'maybe'})]}, 'outcome.status'),
])
def test_validator_rejections_are_specific(overrides, message):
    with pytest.raises(Invalid, match=message):
        validator().validate(report(**overrides))


@pytest.mark.parametrize('overrides,message', [
    ({'headline': '今天 u2 一直在约人'}, 'not the alias u2'),
    ({'highlights': [{'text': '阿青和小白热烈讨论开黑', 'evidence_refs': ['qq:1']}]}, '热烈讨论'),
    ({'highlights': [{'text': '阿青在 qq:1 约人', 'evidence_refs': ['qq:1']}]}, 'message refs'),
    ({'topics': [topic(gist='阿青可能想开黑，小白似乎也想')]}, 'at most one hedge'),
    ({'topics': [topic(gist='这是 inference 的结论')]}, 'certainty code'),
    ({'quotes': [{'ref': 'qq:2', 'context': '体现了小白的热情'}]}, '体现了'),
    ({'titles': [{'member': 'u1', 'title': '组局发起人', 'reason': '一' * 61, 'evidence_refs': ['qq:1']}]}, 'reason must be text'),
])
def test_prose_lint_rejects_what_readers_would_have_to_translate(overrides, message):
    with pytest.raises(Invalid, match=message):
        validator().validate(report(**overrides))


def test_unread_messages_cannot_be_cited_and_drafts_may_be_partial():
    with pytest.raises(Invalid, match='read'):
        validator(read={'qq:1'}).validate(report())
    draft = topic(points=[{'text': '阿青问今晚谁一起开黑', 'evidence_refs': ['qq:1']}], participants=['u1'])
    partial = validator(read={'qq:1'}).validate({'topics': [draft]}, partial=True)
    assert set(partial) == {'topics'}
    assert '安静' in render_empty('2026-10-07')


def test_calendar_buffer_and_dst_history():
    assert completed_days('Asia/Shanghai', datetime(2026, 10, 7, 16, 20, tzinfo=timezone.utc)) == ['2026-10-06', '2026-10-05', '2026-10-04']
    assert completed_days('Asia/Shanghai', datetime(2026, 10, 7, 16, 40, tzinfo=timezone.utc))[0] == '2026-10-07'
    days = previous_days('2026-03-09', 'America/New_York', 2)
    assert [d[0] for d in days] == ['2026-03-07', '2026-03-08']
    assert (days[1][2] - days[1][1]).total_seconds() == 23 * 3600


def test_conversation_identity_is_strict():
    assert conversation('qq', 'group', 123) == ('qq', 'group', '123')
    for bad in [('qq', 'private', '1'), ('qq', 'group', '-5'), ('wechat', 'group', '1'), ('qq', 'group', '01')]:
        with pytest.raises(ValueError):
            conversation(*bad)


class FakeRepo:
    def __init__(self):
        self.config = settings({})
        self.created = []

    def enabled(self):
        return [{'platform': 'qq', 'conversation_type': 'group', 'external_id': '1', 'timezone': 'Asia/Shanghai'}]

    def create(self, *args):
        self.created.append(args)
        return {'status': 'pending'}


def test_scheduler_creates_idempotent_keys_for_enabled_groups_only():
    repo = FakeRepo()
    service = Service(repo, {}, 'a' * 32, 'b' * 32)
    created = asyncio.run(service.schedule_once(datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)))
    assert created == 3
    keys = [args[-1] for args in repo.created]
    assert keys[0] == 'schedule:v1:qq:group:1:2026-10-07:Asia/Shanghai' and len(set(keys)) == 3
    with pytest.raises(ValueError):
        Service(repo, {}, 'a' * 32, 'a' * 32)
    with pytest.raises(ValueError, match='actor'):
        asyncio.run(service.admin('create', {'actor': 'scheduler'}))


def test_settings_bounds():
    config = settings({'CHAT_ANALYSIS_MAX_STEPS': '40'})
    assert config['max_steps'] == 40 and config['model'] == 'deepseek-v4-flash'
    with pytest.raises(ValueError):
        settings({'CHAT_ANALYSIS_MAX_STEPS': '1'})
    with pytest.raises(Exception):
        settings({'CHAT_ANALYSIS_DEFAULT_TIMEZONE': 'Mars/Base'})


class Cursor:
    def __init__(self, log, column):
        self.log, self.column, self.last = log, column, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.log.append((sql, args))
        self.last = sql

    def fetchone(self):
        return {'n': 1 if self.column else 0} if 'information_schema' in self.last else None

    @property
    def lastrowid(self):
        return 1


class Connection:
    def __init__(self, column):
        self.log, self.column = [], column

    def cursor(self):
        return Cursor(self.log, self.column)

    def ping(self, reconnect=True):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.mark.parametrize('column', [True, False])
def test_sender_name_snapshot_is_written_only_when_the_column_exists(column):
    from types import SimpleNamespace
    from igngbot_shared.db import DBHandler
    db = DBHandler(SimpleNamespace(), legacy_compat=False)
    db._conn = Connection(column)
    db.insert_message(group_id=1, sender_id=2, message_content='hi', reply_to_msg_id=None, msg_id='x', sender_name='阿青')
    inserts = [(sql, args) for sql, args in db._conn.log if sql.startswith('INSERT INTO message_logs')]
    assert len(inserts) == 1
    assert ('sender_name' in inserts[0][0]) is column and (('阿青' in inserts[0][1]) is column)


def test_forwarded_records_and_bare_mentions_never_carry_account_numbers():
    forwarded = '[聊天记录]\n  1094950020: 这是什么网站\n  10001: 你马上就能在别的群见到你自己了 13800138000'
    rows = [row(1, 10001, '在吗', 0, sender_name='阿青'),
            row(2, 10002, forwarded, 1, sender_name='小白', message_structure=json.dumps([{'type': 'forward', 'id': 'f', 'children': []}])),
            row(3, 10002, '@10001 @55667788 看这个，2026 年 36 分钟', 2, sender_name='小白')]
    messages, _, manifest = QQGroupSource(None, BOT).normalize(rows, {}, set(), [], {}, 'now', 3)
    text = json.dumps(messages, ensure_ascii=False)
    for secret in ('1094950020', '10001', '55667788', '13800138000'):
        assert secret not in text
    assert '阿青: 你马上就能' in messages[1]['text'] and '[号码]: 这是什么网站' in messages[1]['text']
    assert messages[2]['text'].startswith('@阿青 @[号码]') and '2026 年 36 分钟' in messages[2]['text']
    roster = {p['alias']: p for p in manifest['participants']}
    check = Validator({m['ref'] for m in messages}, roster, {m['ref']: m for m in messages}, 3)
    with pytest.raises(Invalid, match='forwarded record'):
        check.quote({'ref': 'qq:2'}, 0)
    assert check.quote({'ref': 'qq:3'}, 0)['text'].startswith('@阿青')
