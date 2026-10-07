"""Deterministic metrics; none of this module's output is a historian model input."""
from collections import Counter, defaultdict
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from .events import instant


def compute(events, history, start, end, zone):
    players = defaultdict(lambda: {'joins': 0, 'chat': 0, 'deaths': 0, 'kills': 0, 'online_seconds': 0})
    first, active, intervals, presence_seen = {}, {}, [], {}
    seen_players = set()
    warnings, interactions = set(), Counter()
    for event in history:
        uid = event.get('player_uuid')
        at = instant(event['occurred_at'])
        if event['type'] in {'SERVER_START', 'RECORDER_START'}:
            if active:
                warnings.add('restart_without_terminal_events')
            active.clear()  # No fabricated exit time across a restart.
            presence_seen.clear()
            continue
        if event['type'] in {'SERVER_STOP', 'RECORDER_STOP'}:
            for player_id, entered in active.items():
                if at > start:
                    intervals.append((player_id, max(start, entered), min(end, at)))
            active.clear()
            presence_seen.clear()
            warnings.add('recorder_stop_is_not_confirmed_player_quit')
            continue
        if not uid or event.get('is_fake') is True:
            continue
        if event['type'] == 'JOIN':
            first.setdefault(uid, at)
            if uid in active:
                warnings.add('duplicate_join_or_missing_quit')
            active[uid] = at
            presence_seen[uid] = at
        elif event['type'] == 'PRESENCE':
            presence_seen[uid] = at
            if uid not in active:
                active[uid] = at
                warnings.add('presence_without_join_duration_is_lower_bound')
        elif event['type'] in {'QUIT', 'KICK'}:
            entered = active.pop(uid, None)
            presence_seen.pop(uid,None)
            if entered is None:
                if at >= start:
                    warnings.add('missing_join')
            elif at > start:
                intervals.append((uid, max(start, entered), min(end, at)))
        elif uid in active:
            presence_seen[uid] = at
    for uid, entered in active.items():
        last_seen=presence_seen.get(uid)
        closed=end
        if last_seen and last_seen < end-timedelta(seconds=330):
            closed=last_seen
            warnings.add('stale_presence_open_duration_is_lower_bound')
        intervals.append((uid, max(start, entered), closed))
        warnings.add('open_session_at_boundary')
    ticks = []
    hourly = Counter()
    for uid, entered, exited in intervals:
        if exited <= entered:
            continue
        players[uid]['online_seconds'] += (exited - entered).total_seconds()
        ticks.extend([(entered, 1), (exited, -1)])
        cursor = entered
        while cursor < exited:
            local = cursor.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))
            next_hour = min(exited, cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            hourly[local.isoformat(timespec='hours')] += (next_hour - cursor).total_seconds()
            cursor = next_hour
    counts, chat_hours, worlds, sources = Counter(), Counter(), Counter(), Counter()
    for event in events:
        if event.get('time_precision') == 'legacy_second':
            warnings.add('legacy_second_precision')
        uid = event.get('player_uuid')
        if event.get('is_fake') is None and uid:
            warnings.add('historical_human_fake_classification_unknown')
        if event.get('is_fake') is True:
            continue
        facts = event['facts']
        kind = event['type']
        if kind == 'CHAT' and facts.get('cancelled'):
            counts['cancelled_chat'] += 1
            continue
        counts[kind] += 1
        if not uid:
            if event.get('player_name') and kind not in {'RECORDER_START','RECORDER_STOP','SERVER_START','SERVER_STOP'}:
                warnings.add('missing_player_uuid')
            continue
        player = players[uid]
        seen_players.add(uid)
        player['name'] = event.get('player_name') or uid
        if facts.get('world'):
            worlds[facts['world']] += 1
        if kind == 'JOIN':
            player['joins'] += 1
        elif kind == 'CHAT':
            player['chat'] += 1
            sources[facts.get('source', 'unknown')] += 1
            hour = instant(event['occurred_at']).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).isoformat(timespec='hours')
            chat_hours[hour] += 1
        elif kind == 'DEATH':
            player['deaths'] += 1
            killer = facts.get('related_uuid')
            if killer and killer != uid and facts.get('related_entity_type') == 'PLAYER' and facts.get('related_is_fake') is not True:
                if facts.get('related_is_fake') is None:
                    warnings.add('historical_pvp_fake_classification_unknown')
                players[killer]['kills'] += 1
                players[killer].setdefault('name', facts.get('related_name') or killer)
                interactions[(killer, uid)] += 1
    online = peak = 0
    for _, change in sorted(ticks):  # terminal events before joins at the same instant
        online += change
        peak = max(peak, online)
    new = [uid for uid, at in first.items() if start <= at < end]
    # Backward retention is available for completed periods, forward D1/D7 is not guessed.
    today = {e.get('player_uuid') for e in events if e['type'] == 'JOIN' and e.get('is_fake') is not True} - {None}
    retained = {}
    for days in (1, 7, 30):
        prior = {e.get('player_uuid') for e in history if e['type'] == 'JOIN' and e.get('is_fake') is not True and
                 start - timedelta(days=days) <= instant(e['occurred_at']) < end - timedelta(days=days)} - {None}
        retained[f'overlap_previous_{days}d'] = {'cohort': len(prior), 'returned': len(today & prior),
                                                'rate': len(today & prior) / len(prior) if prior else None}
        new_cohort = {uid for uid, at in first.items() if start - timedelta(days=days) <= at < end - timedelta(days=days)}
        retained[f'new_observed_{days}d'] = {'cohort':len(new_cohort), 'returned':len(today & new_cohort),
                                          'rate':len(today & new_cohort)/len(new_cohort) if new_cohort else None,
                                          'definition':'first observed in shifted period, JOIN in current period; 90-day bounded history'}
    seen_players.update(uid for uid, entered, exited in intervals if exited > entered)
    rankings = {key: [{'uuid':uid,'name':values.get('name',uid),'value':values[key]} for uid,values in
                     sorted(players.items(),key=lambda p:(-p[1][key],p[0]))] for key in ('online_seconds','joins','chat','deaths','kills')}
    return {'algorithm_version': 1, 'timezone': zone, 'independent_players': len(seen_players),
            'joins': counts['JOIN'], 'chat': counts['CHAT'], 'cancelled_chat': counts['cancelled_chat'],
            'deaths': counts['DEATH'], 'pvp_kills': sum(interactions.values()), 'peak_online': peak,
            'online_seconds': sum(p['online_seconds'] for p in players.values()),
            'new_observed_players': len(new), 'new_player_definition': 'first JOIN in available 90-day history, not registration',
            'players': [{'uuid': uid, **values} for uid, values in sorted(players.items(), key=lambda p: (-p[1]['online_seconds'], p[0]))],
            'hourly': [{'hour': hour, 'online_seconds': hourly[hour], 'chat': chat_hours[hour]} for hour in sorted(set(hourly) | set(chat_hours))],
            'pvp_edges': [{'from': a, 'to': b, 'kills': n} for (a, b), n in sorted(interactions.items())],
            'retention': retained, 'forward_retention': 'not_matured',
            'rankings':rankings, 'distributions':{'world_events':dict(worlds),'chat_sources':dict(sources),'event_types':dict(counts)},
            'quality': {'status': 'partial' if warnings else 'observed', 'warnings': sorted(warnings),
                        'note': 'event-derived; queue loss, abrupt shutdown and history start can limit accuracy'}}
