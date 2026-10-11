"""Opt-in public feeds and Bilibili subtitles, with pinned public DNS and no redirects."""
import asyncio
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import aiohttp
from .models import public_url, identifier, text, material


def source_config(value):
    if not isinstance(value, dict):
        raise ValueError('source object required')
    kind = value.get('kind', 'json')
    if kind not in {'json', 'rss', 'bilibili'}:
        raise ValueError('unsupported source kind')
    seconds = value.get('poll_seconds', 21600)
    if type(seconds) is not int or not 600 <= seconds <= 604800:
        raise ValueError('invalid poll interval')
    hosts = value.get('allowed_hosts', [])
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 12:
        raise ValueError('explicit source host allowlist required')
    hosts = [urlsplit(public_url('https://' + text(host, 240))).hostname for host in hosts]
    url = public_url(value['feed_url'])
    if urlsplit(url).hostname not in hosts:
        raise ValueError('feed host outside allowlist')
    creator = text(value.get('creator_id', value['id']), 80)
    if kind == 'bilibili' and not re.fullmatch(r'[1-9][0-9]{0,19}', creator):
        raise ValueError('Bilibili creator MID required')
    return {'id': identifier(value['id']), 'kind': kind, 'creator_id': creator,
            'feed_url': url, 'allowed_hosts': hosts, 'poll_seconds': seconds,
            'enabled': value.get('enabled') is True, 'media_enabled': value.get('media_enabled') is True}


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        result = []
        for af, _, _, _, addr in addresses:
            ip = ipaddress.ip_address(addr[0])
            if not ip.is_global:
                raise ValueError('private/reserved DNS answer rejected')
            result.append({'hostname': host, 'host': str(ip), 'port': port, 'family': af, 'proto': 0, 'flags': 0})
        if not result:
            raise ValueError('no public DNS answers')
        return result

    async def close(self):
        pass


async def fetch_public(url, hosts, limit=2 * 1024 * 1024):
    public_url(url)
    if urlsplit(url).hostname not in hosts:
        raise ValueError('download host outside allowlist')
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                     timeout=aiohttp.ClientTimeout(total=45)) as session:
        async with session.get(url, allow_redirects=False, headers={'User-Agent': 'YunYingSlang/1.0'}) as response:
            if response.status != 200:
                raise ValueError('public source unavailable (no redirect or authentication bypass)')
            if response.content_length and response.content_length > limit:
                raise ValueError('source too large')
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('source too large')
            return bytes(data)


def feed_items(data, kind):
    if kind == 'json':
        value = json.loads(data)
        items = value.get('items', []) if isinstance(value, dict) else value
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError('bounded JSON feed items required')
        return items[:20]
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('XML entities rejected')
    root = ET.fromstring(data)
    result = []
    for item in root.findall('.//item')[:20]:
        raw = item.findtext('pubDate')
        date = parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat() if raw else None
        result.append({'url': item.findtext('link'), 'title': item.findtext('title'), 'published_at': date})
    return result


def subtitle_segments(data, format='json'):
    if format == 'json':
        value = json.loads(data)
        rows = value.get('body', value.get('segments', [])) if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValueError('subtitle rows required')
        result = [{'id': f'segment:{index}', 'start': row.get('from', row.get('start')), 'end': row.get('to', row.get('end')),
                   'excerpt': row.get('content', row.get('text', row.get('excerpt', '')))} for index, row in enumerate(rows)]
    else:
        # WebVTT/SRT timestamps; ignore cue IDs, preserve time anchors.
        body = data.decode('utf-8-sig')
        pattern = r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)'
        seconds = lambda v: sum(float(n) * factor for n, factor in zip(v.replace(',', '.').split(':'), (3600, 60, 1)))
        result = [{'id': f'segment:{i}', 'start': seconds(a), 'end': seconds(b), 'excerpt': re.sub(r'<[^>]+>', '', c).strip()}
                  for i, (a, b, c) in enumerate(re.findall(pattern, body, re.S))]
    # Coalesce adjacent cues instead of silently truncating long videos.
    merged = []
    for row in result:
        if not row['excerpt']:
            continue
        if len(row['excerpt']) > 1200:
            raise ValueError('subtitle cue too long; split material explicitly')
        if merged and len(merged[-1]['excerpt']) + len(row['excerpt']) < 1000:
            merged[-1]['excerpt'] += '\n' + row['excerpt']
            merged[-1]['end'] = row['end']
        else:
            merged.append({**row, 'id': f'segment:{len(merged)}'})
    if not merged or len(merged) > 120:
        raise ValueError('subtitle capacity exceeded or empty; split material explicitly')
    return merged


class SourceReader:
    def __init__(self, fetch=fetch_public, media=None):
        self.fetch, self.media = fetch, media

    async def discover(self, config):
        data = await self.fetch(config['feed_url'], config['allowed_hosts'])
        return feed_items(data, 'rss' if config['kind'] in {'rss', 'bilibili'} else 'json')

    async def snapshot(self, config, item):
        url = public_url(item['url'])
        title = item.get('title') or url
        published = item.get('published_at')
        segments = item.get('segments')
        if config['kind'] == 'bilibili':
            match = re.fullmatch(r'/video/(BV[a-zA-Z0-9]{10})/?', urlsplit(url).path)
            if urlsplit(url).hostname not in {'www.bilibili.com', 'bilibili.com'} or not match:
                raise ValueError('Bilibili video URL required')
            bvid = match[1]
            info = json.loads(await self.fetch(f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}', config['allowed_hosts']))
            data = info.get('data', {})
            if info.get('code') != 0 or str(data.get('owner', {}).get('mid')) != config['creator_id']:
                raise ValueError('video creator mismatch or video unavailable')
            title = data['title']
            published = datetime.fromtimestamp(data['pubdate'], timezone.utc).isoformat()
            player = json.loads(await self.fetch(f'https://api.bilibili.com/x/player/v2?cid={data["cid"]}&bvid={bvid}', config['allowed_hosts']))
            subtitles = player.get('data', {}).get('subtitle', {}).get('subtitles', [])
            if not subtitles:
                raise ValueError('public subtitle unavailable; use an authorized material import')
            subtitle = next((s for s in subtitles if str(s.get('lan', '')).startswith('zh')), subtitles[0])
            caption_url = subtitle['subtitle_url']
            if caption_url.startswith('//'):
                caption_url = 'https:' + caption_url
            segments = subtitle_segments(await self.fetch(caption_url, config['allowed_hosts']))
        elif item.get('caption_url'):
            segments = subtitle_segments(await self.fetch(item['caption_url'], config['allowed_hosts']), item.get('caption_format', 'json'))
        elif not segments and item.get('video_url') and config['media_enabled'] and self.media:
            blob = await self.fetch(item['video_url'], config['allowed_hosts'], limit=50 * 1024 * 1024)
            segments = await self.media.analyze(blob, visual=item.get('visual_required') is True)
        if not segments:
            raise ValueError('no actual subtitles/material; title and description are not evidence')
        return material({'url': url, 'title': title, 'published_at': published,
                         'origin': config['kind'] + ':' + config['creator_id'], 'segments': segments})
