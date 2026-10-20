import json
from datetime import datetime
import pytest
from igngbot_v4.historian import readable
from igngbot_v4.historian.events import normalize

ZONE = 'Asia/Shanghai'
STEVE = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
ALEX = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


def row(n, kind, minute=0, uid=STEVE, name='Steve', **fields):
    # 2026-10-14 13:00 UTC is 周三 21:00 in Asia/Shanghai.
    return normalize('player_event_logs', {'id': n, 'server_id': 1, 'occurred_at': datetime(2026, 10, 14, 13, minute),
                                           'event_type': kind, 'player_uuid': uid, 'player_name': name,
                                           'boot_id': 'boot-secret', 'capture_seq': n, 'is_fake': False, **fields})


def chat(n, text, minute=0, uid=STEVE, name='Steve', **fields):
    return normalize('chat_messages', {'id': n, 'server_id': 1, 'sent_at': datetime(2026, 10, 14, 13, minute),
                                       'player_uuid': uid, 'player_name': name, 'content': text, 'source': 'paper',
                                       'cancelled': False, 'boot_id': 'boot-secret', 'capture_seq': n, **fields})


def test_dictionary_translates_codes_and_keeps_unknown_codes_in_brackets():
    death = row(1, 'DEATH', 3, related_entity_type='ZOMBIE', damage_type='minecraft:mob_attack', cause='ENTITY_ATTACK',
                world='world', x=120.4, y=64, z=-30.6)
    assert readable.describe(death, ZONE) == '10-14 周三 21:03 Steve 被僵尸杀死（主世界 120,64,-30）'
    assert readable.describe(row(2, 'DEATH', cause='LAVA', damage_type='minecraft:lava'), ZONE).endswith('Steve 掉进岩浆烧死')
    pvp = row(3, 'DEATH', related_entity_type='PLAYER', related_uuid=ALEX, related_name='Alex', damage_type='minecraft:arrow')
    assert readable.describe(pvp, ZONE).endswith('Steve 被 Alex 射杀')
    assert readable.describe(row(4, 'JOIN'), ZONE) == '10-14 周三 21:00 Steve 进服'
    assert readable.describe(row(5, 'QUIT', reason='TIMED_OUT', cause='TIMED_OUT'), ZONE).endswith('Steve 退出（连接超时）')
    assert readable.describe(chat(6, '有人吗'), ZONE).endswith('Steve 说：有人吗')
    assert '没有发出' in readable.describe(chat(7, '刷屏', cancelled=True), ZONE)
    assert readable.describe(row(8, 'ADVANCEMENT', reason='minecraft:story/mine_diamond'), ZONE).endswith('达成进度「钻石！」')
    assert readable.describe(row(9, 'WORLD_CHANGE', reason='from:world', world='world_the_end'), ZONE).endswith('从主世界来到末地')
    stop = row(10, 'SERVER_STOP', uid=None, name=None)
    assert readable.describe(stop, ZONE) == '10-14 周三 21:00 服务器停止'
    # Unknown vocabulary stays visible but bracketed instead of being guessed.
    assert readable.describe(row(11, 'DEATH', damage_type='minecraft:new_thing'), ZONE).endswith('死亡（minecraft:new_thing）')
    assert readable.describe(row(12, 'DEATH', related_entity_type='SNIFFER_KING'), ZONE).endswith('被（SNIFFER_KING）杀死')
    assert readable.describe(row(13, 'DEATH', world='world_farm'), ZONE).endswith('世界（world_farm））')
    assert readable.describe(row(14, 'TELEPORT'), ZONE).endswith('Steve 触发事件（TELEPORT）')


def test_model_view_has_line_ref_and_aliases_but_no_identifiers():
    events = [row(1, 'JOIN'), row(2, 'DEATH', 5, related_entity_type='PLAYER', related_uuid=ALEX, related_name='Alex'),
              chat(3, '你干嘛', 6, uid=ALEX, name='Alex')]
    players = readable.roster(events)
    assert [(p['alias'], p['name']) for p in players] == [('p1', 'Steve'), ('p2', 'Alex')]
    people = readable.Roster(players)
    views = [readable.view(event, people, ZONE) for event in events]
    assert views[1] == {'ref': 'mc:1:player_event_logs:2', 'line': '10-14 周三 21:05 Steve 被 Alex 杀死', 'who': ['p1', 'p2']}
    dumped = json.dumps(views, ensure_ascii=False)
    for secret in (STEVE, ALEX, 'boot-secret', 'capture_seq', 'content_hash', 'DEATH', 'player_uuid'):
        assert secret not in dumped
    assert people.resolve('p2')['uuid'] == ALEX and people.resolve('alex')['uuid'] == ALEX
    with pytest.raises(ValueError):
        people.resolve('p9')


def submission(**story):
    base = {'title': 'Steve 被 Alex 打死', 'gist': 'Steve 周三晚上进服 5 分钟后被 Alex 杀死，之后两人在聊天里吵了一句。',
            'timeline': [{'text': 'Steve 进服', 'evidence_event_ids': ['mc:1:player_event_logs:1']},
                         {'text': 'Alex 在聊天里问 Steve 在干什么', 'evidence_event_ids': ['mc:1:chat_messages:3']},
                         {'text': 'Steve 被 Alex 杀死', 'evidence_event_ids': ['mc:1:player_event_logs:2']}],
            'people': ['p1', 'p2'], 'outcome': {'status': 'unknown', 'text': '记录里看不出两人后来有没有和好'}, 'certainty': 'fact'}
    return {'headline': 'Alex 周三晚上杀死了刚进服的 Steve',
            'highlights': [{'text': 'Steve 周三 21:00 进服，5 分钟后被 Alex 杀死', 'evidence_event_ids': ['mc:1:player_event_logs:2']},
                           {'text': 'Alex 随后在聊天里说了一句“你干嘛”', 'evidence_event_ids': ['mc:1:chat_messages:3']},
                           {'text': '本周只有 Steve 和 Alex 两名玩家上线', 'evidence_event_ids': ['mc:1:player_event_logs:1']}],
            'stories': [{**base, **story}],
            'server_notes': [{'text': '周三 21:30 服务器停止', 'evidence_event_ids': ['mc:1:player_event_logs:10']}]}


PEOPLE = readable.Roster([{'alias': 'p1', 'uuid': STEVE, 'name': 'Steve'}, {'alias': 'p2', 'uuid': ALEX, 'name': 'Alex'}])


@pytest.mark.parametrize('field,value,reason', [
    ('gist', 'p1 被 p2 杀死', '玩家代号'),
    ('gist', f'Steve（{STEVE}）被杀', 'UUID'),
    ('gist', 'Steve 被杀，见 mc:1:player_event_logs:2', '事件编号'),
    ('gist', 'Steve 因为 LAVA 死了', '英文代码'),
    ('gist', 'Steve 死于 minecraft:lava', '英文代码'),
    ('gist', 'Steve 被杀（fact）', 'fact/inference'),
    ('gist', '两人展开了激烈对决', '空话'),
    ('gist', 'Steve 可能是挂机，似乎没注意到 Alex', '可能/似乎'),
    ('title', 'Steve 和 Alex 这一周之间的恩怨从头到尾', '超过 20 字'),
])
def test_lint_rejects_unreadable_prose_with_a_specific_reason(field, value, reason):
    with pytest.raises(ValueError) as error:
        readable.validate(submission(**{field: value}), PEOPLE, 4, True)
    assert reason in str(error.value) and f'stories[0].{field}' in str(error.value)


def test_lint_allows_player_names_that_look_like_codes_and_rejects_unknown_aliases():
    people = readable.Roster([{'alias': 'p1', 'uuid': STEVE, 'name': 'CREEPER_FAN'}])
    data = submission(gist='CREEPER_FAN 周三被 TNT 炸死了一次', people=['p1'])
    assert readable.validate(data, people, 4, True)['stories'][0]['people'] == [{'alias': 'p1', 'name': 'CREEPER_FAN'}]
    with pytest.raises(ValueError, match='p7 不在本周名单'):
        readable.validate(submission(people=['p7']), PEOPLE, 4, True)
    with pytest.raises(ValueError, match='v2 report required'):
        readable.validate({'observations': []}, PEOPLE, 4, True)
    with pytest.raises(ValueError, match='stories 需要 1~12 条'):
        readable.validate({**submission(), 'stories': []}, PEOPLE, 4, True)
    # Drafts may be partial; an empty period needs no stories.
    assert readable.validate({'stories': []}, PEOPLE, 4, False)['stories'] == []
    assert readable.validate({'headline': '本周没有记录到任何事件'}, PEOPLE, 0, True)['highlights'] == []


def test_v2_derives_v1_observations_and_markdown_without_event_ids():
    report = readable.validate(submission(), PEOPLE, 4, True)
    refs = readable.references(report)
    assert 'mc:1:chat_messages:3' in refs and len(refs) == len(set(refs))
    times = {'mc:1:player_event_logs:1': datetime(2026, 10, 14, 13, 0), 'mc:1:player_event_logs:2': datetime(2026, 10, 14, 13, 5),
             'mc:1:chat_messages:3': datetime(2026, 10, 14, 13, 6), 'mc:1:player_event_logs:10': datetime(2026, 10, 14, 13, 30)}
    report = readable.timed(report, times, ZONE)
    story = report['stories'][0]
    # Server-computed times; the timeline is put back into chronological order.
    assert [entry['at'] for entry in story['timeline']] == ['周三 21:00', '周三 21:05', '周三 21:06']
    assert story['timeline'][1]['text'] == 'Steve 被 Alex 杀死'
    assert set(story['evidence_event_ids']) == {'mc:1:player_event_logs:1', 'mc:1:player_event_logs:2', 'mc:1:chat_messages:3'}
    observations = readable.observations(report)
    assert [o['title'] for o in observations] == ['本周概览', 'Steve 被 Alex 打死', '服务器状况']
    assert all(o['certainty'] in readable.CERTAINTY and o['evidence_event_ids'] for o in observations)
    assert '周三 21:05 Steve 被 Alex 杀死' in observations[1]['summary'] and '结果不明' in observations[1]['summary']
    text = readable.markdown(report, 1, '2026-10-12', ZONE, 4)
    assert 'mc:' not in text and '证据' not in text and STEVE not in text
    order = [text.index(part) for part in ('# Alex 周三晚上杀死了刚进服的 Steve', '## 本周要点', '## Steve 被 Alex 打死',
                                           '- 周三 21:05 Steve 被 Alex 杀死', '结果：', '## 服务器状况')]
    assert order == sorted(order)
    assert readable.span(datetime(2026, 10, 14, 13, 0), datetime(2026, 10, 14, 17, 10), ZONE) == '周三 21:00–周四 01:10'
    assert readable.span(datetime(2026, 10, 14, 13, 0), datetime(2026, 10, 14, 13, 40), ZONE) == '周三 21:00–21:40'


def test_previous_digest_prefers_v2_and_falls_back_to_observations():
    report = readable.timed(readable.validate(submission(), PEOPLE, 4, True), {
        ref: datetime(2026, 10, 14, 13) for ref in readable.references(readable.validate(submission(), PEOPLE, 4, True))}, ZONE)
    digest = readable.digest(report, [])
    assert digest['headline'] == 'Alex 周三晚上杀死了刚进服的 Steve' and digest['story_titles'] == ['Steve 被 Alex 打死']
    assert 'mc:' not in json.dumps(digest, ensure_ascii=False)
    old = readable.digest(None, [{'title': '旧观察', 'summary': '旧正文', 'certainty': 'fact', 'evidence_event_ids': ['mc:1:x:1']}])
    assert old == {'version': 1, 'observations': [{'title': '旧观察', 'summary': '旧正文', 'certainty': 'fact'}]}
