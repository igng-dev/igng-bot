from datetime import datetime, timezone
import pytest
from igngbot_v4.historian.period import period, completed_periods
from igngbot_v4.historian.events import normalize, ordered, content_hash
from igngbot_v4.historian.statistics import compute


def event(kind, at, uid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', **facts):
    return {'type': kind, 'occurred_at': at + 'Z', 'player_uuid': uid,
            'player_name': 'Player', 'is_fake': False, 'facts': facts, 'time_precision': 'millisecond'}


def test_period_is_iana_calendar_not_machine_timezone():
    start, end = period('daily', '2026-10-07', 'Asia/Shanghai', datetime(2026, 10, 9, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 6, 16)
    assert end == datetime(2026, 10, 7, 16)
    # DST is a real 23-hour day, not UTC+N arithmetic.
    start, end = period('daily', '2026-03-08', 'America/New_York')
    assert (end-start).total_seconds() == 23*3600
    with pytest.raises(ValueError):
        period('weekly', '2026-10-07', 'Asia/Shanghai')
    with pytest.raises(ValueError):
        period('daily', '2099-01-01', 'Asia/Shanghai')


def test_schedule_buffer_and_week_boundary():
    periods = completed_periods('Asia/Shanghai', datetime(2026, 10, 11, 16, 5, tzinfo=timezone.utc))
    assert periods == [('daily', '2026-10-10'), ('weekly', '2026-09-28')]
    periods = completed_periods('Asia/Shanghai', datetime(2026, 10, 11, 16, 15, tzinfo=timezone.utc))
    assert periods[-1] == ('weekly', '2026-10-05')


def test_unified_facts_preserve_refs_and_remove_ip():
    row = dict(id=42, server_id=2, player_uuid='uuid', player_name='A', sent_at=datetime(2026, 1, 1),
               content='原话', original_content='原话', source='paper', cancelled=True,
               boot_id='boot', capture_seq=5, is_fake=False, ip_address='private')
    chat = normalize('chat_messages', row)
    death = normalize('player_event_logs', {**row, 'id': 43, 'occurred_at': row['sent_at'], 'event_type': 'DEATH', 'capture_seq': 4})
    assert ordered([chat, death]) == [death, chat]
    assert chat['event_id'] == 'mc:2:chat_messages:42'
    assert chat['facts']['cancelled'] is True
    assert 'ip_address' not in str(chat)
    assert content_hash(chat) != content_hash({**chat, 'player_name': 'B'})


def test_midnight_carry_in_kick_pvp_cancelled_chat_and_fake_exclusion():
    start, end = datetime(2026, 10, 7), datetime(2026, 10, 8)
    history = [event('JOIN', '2026-10-06T23:00:00'), event('KICK', '2026-10-07T01:00:00')]
    events = [history[-1], event('CHAT', '2026-10-07T00:01:00', cancelled=True),
              event('DEATH', '2026-10-07T00:02:00', related_uuid='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', related_entity_type='PLAYER')]
    fake = event('CHAT', '2026-10-07T00:03:00'); fake['is_fake'] = True
    stats = compute([*events, fake], history, start, end, 'Asia/Shanghai')
    assert stats['online_seconds'] == 3600
    assert stats['peak_online'] == 1
    assert stats['chat'] == 0 and stats['cancelled_chat'] == 1
    assert stats['pvp_kills'] == 1
    assert stats['hourly'][0]['hour'].endswith('+08:00')
    assert stats['forward_retention'] == 'not_matured'


def test_missing_history_is_not_reported_as_exact():
    start, end = datetime(2026, 10, 7), datetime(2026, 10, 8)
    stats = compute([], [event('QUIT', '2026-10-07T01:00:00')], start, end, 'Asia/Shanghai')
    assert stats['online_seconds'] == 0
    assert 'missing_join' in stats['quality']['warnings']


def test_fake_killer_does_not_enter_human_pvp_and_stale_presence_stops_carry_out():
    start,end=datetime(2026,10,7),datetime(2026,10,8)
    join=event('JOIN','2026-10-07T00:00:00')
    presence=event('PRESENCE','2026-10-07T00:05:00')
    death=event('DEATH','2026-10-07T00:06:00',related_uuid='fake',related_entity_type='PLAYER',related_is_fake=True)
    stats=compute([join,presence,death],[join,presence,death],start,end,'Asia/Shanghai')
    assert stats['pvp_kills']==0
    assert stats['online_seconds']==360
    assert 'stale_presence_open_duration_is_lower_bound' in stats['quality']['warnings']


def test_command_analytics_death_causes_and_session_durations():
    start, end = datetime(2026, 10, 7), datetime(2026, 10, 8)
    join = event('JOIN', '2026-10-07T00:00:00')
    cmd1 = event('COMMAND', '2026-10-07T00:01:00', reason='/home bed')
    cmd2 = event('COMMAND', '2026-10-07T00:02:00', reason='/tpa Friend')
    death = event('DEATH', '2026-10-07T00:05:00', cause='FALL', damage_type='minecraft:fall')
    quit_ev = event('QUIT', '2026-10-07T00:10:00')
    stats = compute([join, cmd1, cmd2, death, quit_ev], [join, quit_ev], start, end, 'Asia/Shanghai')
    assert stats['commands'] == 2
    assert stats['top_commands'][0]['command'] == '/home'
    assert any(c['command'] == '/tpa' for c in stats['top_commands'])
    assert stats['death_causes'][0]['cause'] == 'minecraft:fall'
    assert stats['session_durations']['5-15m'] == 1
    assert stats['median_first_death_seconds'] == 300.0
    assert stats['rankings']['commands'][0]['value'] == 2
