"""Deterministic daily chat metrics. None of this module's output is a model input."""
import re
from collections import Counter, defaultdict
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from .source import instant

ALGORITHM_VERSION = 1
URL = re.compile(r'https?://[^\s<>"\'，。！？、）)]+', re.I)
# Placeholders the parser renders for media; they are not words someone typed.
LABEL = re.compile(r'\[(?:图片|视频|语音|文件[^\]]*|表情[^\]]*|贴纸[^\]]*|聊天记录|转发消息|语音转写)\]')


def _local(at, zone):
    return instant(at).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))


def compute(messages, context, manifest, start, end, zone):
    participants = {p['alias']: p for p in manifest['participants']}
    human = [m for m in messages if not m['is_bot']]
    per = defaultdict(lambda: {'messages': 0, 'characters': 0, 'replies_sent': 0, 'replies_received': 0,
                               'mentions_sent': 0, 'mentions_received': 0, 'media': 0, 'first_at': None, 'last_at': None})
    hourly, kinds, emoji, domains = Counter(), Counter(), Counter(), Counter()
    reply_edges, mention_edges = Counter(), Counter()
    minute = Counter()
    lengths = []
    for message in human:
        alias, local = message['sender'], _local(message['at'], zone)
        stats = per[alias]
        stats['messages'] += 1
        stats['first_at'] = stats['first_at'] or message['at']
        stats['last_at'] = message['at']
        typed = LABEL.sub('', message['text']).strip()
        stats['characters'] += len(typed)
        if typed:
            lengths.append(len(typed))
        if message['kind'] not in {'text'}:
            stats['media'] += 1
        hourly[local.hour] += 1
        kinds[message['kind']] += 1
        emoji.update(message.get('emoji', []))
        minute[local.replace(second=0, microsecond=0)] += 1
        for url in URL.findall(message['text']):
            host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
            if host:
                domains[host] += 1
        target = (message.get('reply_to') or {}).get('to')
        if target and target != alias:
            reply_edges[(alias, target)] += 1
            stats['replies_sent'] += 1
            per[target]['replies_received'] += 1
        for mentioned in message.get('mentions', []):
            if mentioned not in {alias, 'all'}:
                mention_edges[(alias, mentioned)] += 1
                stats['mentions_sent'] += 1
                per[mentioned]['mentions_received'] += 1
    bot_messages = [m for m in messages if m['is_bot']]
    bot_addressed = sum(1 for m in human if _addresses_bot(m))
    # Busiest rolling 10-minute window and longest quiet gap between consecutive human messages.
    times = sorted(_local(m['at'], zone) for m in human)
    burst, left = {'start': None, 'messages': 0}, 0
    for right, at in enumerate(times):
        while at - times[left] >= timedelta(minutes=10):
            left += 1
        if right - left + 1 > burst['messages']:
            burst = {'start': times[left].isoformat(timespec='minutes'), 'messages': right - left + 1}
    gap = {'from': None, 'to': None, 'minutes': 0}
    for before, after in zip(times, times[1:]):
        minutes = (after - before).total_seconds() / 60
        if minutes > gap['minutes']:
            gap = {'from': before.isoformat(timespec='minutes'), 'to': after.isoformat(timespec='minutes'), 'minutes': round(minutes, 1)}
    trend = context.get('trend', [])
    recent = [d['messages'] for d in trend]
    average = sum(recent) / len(recent) if recent else None
    speakers = [a for a, s in per.items() if s['messages'] and not participants.get(a, {}).get('is_bot')]
    name_sources = Counter(context.get('name_sources', {}).get(a) for a in speakers)
    warnings = set()
    if name_sources.get('masked'):
        warnings.add('display_name_unknown_for_some_members')
    if context.get('recalled'):
        warnings.add('recalled_messages_excluded')
    if any(m.get('truncated') for m in human):
        warnings.add('very_long_messages_truncated')
    if not recent or all(value == 0 for value in recent):
        warnings.add('no_prior_week_baseline')

    def member(alias, values):
        info = participants.get(alias, {})
        return {'alias': alias, 'name': info.get('name') or alias, **values}

    ranking = sorted(((a, s) for a, s in per.items() if s['messages']), key=lambda item: (-item[1]['messages'], item[0]))
    peak_hour = max(range(24), key=lambda h: (hourly[h], -h)) if human else None
    return {
        'algorithm_version': ALGORITHM_VERSION, 'timezone': zone,
        'period': {'from': start.isoformat() + 'Z', 'to': end.isoformat() + 'Z'},
        'messages': len(human), 'speakers': len(speakers), 'bot_messages': len(bot_messages),
        'bot_addressed': bot_addressed, 'recalled': context.get('recalled', 0),
        'new_speakers': [member(a, {}) for a in context.get('new_speakers', [])],
        'new_speaker_definition': 'no message in this group during the previous 90 days of available history',
        'characters': sum(lengths), 'average_length': round(sum(lengths) / len(lengths), 1) if lengths else None,
        'kinds': dict(kinds), 'hourly': [{'hour': h, 'messages': hourly[h]} for h in range(24)],
        'peak_hour': peak_hour, 'busiest_window': burst, 'longest_gap': gap,
        'top_members': [member(a, s) for a, s in ranking[:30]],
        'reply_edges': [{'from': a, 'to': b, 'count': n} for (a, b), n in sorted(reply_edges.items(), key=lambda x: (-x[1], x[0]))[:60]],
        'mention_edges': [{'from': a, 'to': b, 'count': n} for (a, b), n in sorted(mention_edges.items(), key=lambda x: (-x[1], x[0]))[:60]],
        'top_emoji': [{'token': k, 'count': v} for k, v in emoji.most_common(15)],
        'top_domains': [{'domain': k, 'count': v} for k, v in domains.most_common(10)],
        'trend': trend, 'previous_7d_average': round(average, 1) if average is not None else None,
        'change_vs_average': round((len(human) - average) / average, 3) if average else None,
        'quality': {'status': 'partial' if warnings - {'no_prior_week_baseline'} else 'observed', 'warnings': sorted(warnings),
                    'note': 'recorded group messages only; ingest outages and recalls reduce coverage'},
    }


def _addresses_bot(message):
    return 'bot' in message.get('mentions', []) or (message.get('reply_to') or {}).get('to') == 'bot'
