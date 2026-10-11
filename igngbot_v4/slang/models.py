"""Bounded data contracts. External prose is evidence, never authority."""
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

CONTEXTS = {'casual', 'gaming', 'anime', 'technical', 'support', 'conflict', 'general'}
STATUSES = {'candidate', 'published', 'disputed', 'disabled'}
LIFECYCLES = {'unknown', 'rising', 'active', 'fading', 'nostalgic'}
USAGES = {'common', 'occasional', 'strong', 'understand_only'}
RISK_FLAGS = {'offensive', 'harassment', 'discrimination', 'tragedy', 'controversial'}
SECRET = re.compile(r'\b(?:sk-[a-zA-Z0-9_-]{12,}|Bearer\s+\S+|password\s*[=:]|api[_ -]?key\s*[=:])', re.I)
INSTRUCTION = re.compile(r'忽略.{0,8}(?:指令|规则|以上)|ignore previous|system prompt|调用.{0,6}(?:shell|工具)|```', re.I)


def text(value, limit, optional=False):
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError('invalid bounded text')
    value = re.sub(r'[\x00-\x1f]+', ' ', value).strip()
    if SECRET.search(value):
        raise ValueError('credential-shaped content rejected')
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._:-]{1,79}', value):
        raise ValueError('invalid identifier')
    return value


def choice(value, options):
    if value not in options:
        raise ValueError('invalid enum')
    return value


def strings(value, count=12, size=120):
    if not isinstance(value, list) or len(value) > count:
        raise ValueError('invalid bounded list')
    return list(dict.fromkeys(text(item, size) for item in value))


def stamp(value):
    if value is None:
        return None
    value = text(value, 40)
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt > datetime.now(timezone.utc):
        raise ValueError('timestamp must be past UTC/offset time')
    return dt.astimezone(timezone.utc).isoformat()


def public_url(value):
    value = text(value, 1000)
    u = urlsplit(value)
    if u.scheme not in {'https', 'http'} or not u.hostname or u.username or u.password:
        raise ValueError('public HTTP(S) source required')
    host = u.hostname.lower().rstrip('.')
    if host == 'localhost' or '.' not in host or host.endswith(('.local', '.internal')):
        raise ValueError('private source rejected')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError('private source rejected')
    if u.port not in {None, 80, 443}:
        raise ValueError('source port rejected')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def source(value):
    if not isinstance(value, dict):
        raise ValueError('source object required')
    result = {'id': identifier(value['id']), 'url': public_url(value['url']),
              'excerpt': text(value.get('excerpt', ''), 1200, True),
              'origin': text(value.get('origin', value['id']), 120),
              'verified': value.get('verified') is True,
              'kind': choice(value.get('kind', 'subtitle'), {'subtitle', 'asr', 'vision', 'reference', 'owner_review'}),
              'published_at': stamp(value.get('published_at')),
              'start': value.get('start'), 'end': value.get('end')}
    for key in ('start', 'end'):
        if result[key] is not None and (isinstance(result[key], bool) or not isinstance(result[key], (int, float)) or not 0 <= result[key] <= 86400):
            raise ValueError('invalid source time')
    if result['start'] is not None and result['end'] is not None and result['end'] < result['start']:
        raise ValueError('invalid source interval')
    result['content_hash'] = digest({'excerpt': result['excerpt'], 'origin': result['origin']})
    return result


def entry(value):
    if not isinstance(value, dict):
        raise ValueError('entry object required')
    aliases = value.get('aliases', [])
    if not isinstance(aliases, list) or not 1 <= len(aliases) <= 24:
        raise ValueError('1..24 aliases required')
    normalized = []
    for alias in aliases:
        if isinstance(alias, str):
            alias = {'text': alias}
        normalized.append({'text': text(alias['text'], 64),
                           'requires_any': strings(alias.get('requires_any', [])),
                           'excludes_any': strings(alias.get('excludes_any', []))})
    sources = value.get('sources', [])
    if not isinstance(sources, list) or not 1 <= len(sources) <= 12:
        raise ValueError('1..12 sources required')
    result = {'id': identifier(value['id']), 'name': text(value['name'], 80),
              'meaning': text(value['meaning'], 800), 'tone': text(value.get('tone', '依语境判断'), 120),
              'aliases': normalized, 'contexts': strings(value.get('contexts', ['casual']), 7, 20),
              'topics': strings(value.get('topics', [])), 'examples': strings(value.get('examples', []), 4, 240),
              'avoid_examples': strings(value.get('avoid_examples', []), 4, 240),
              'usage': choice(value.get('usage', 'understand_only'), USAGES),
              'lifecycle': choice(value.get('lifecycle', 'unknown'), LIFECYCLES),
              'risks': strings(value.get('risks', []), 5, 30),
              'confidence': choice(value.get('confidence', 'low'), {'low', 'medium', 'high'}),
              'observed_at': stamp(value.get('observed_at')), 'verified_at': stamp(value.get('verified_at')),
              'sources': [source(s) for s in sources]}
    if not set(result['contexts']) <= CONTEXTS or not set(result['risks']) <= RISK_FLAGS:
        raise ValueError('invalid context/risk')
    if INSTRUCTION.search(json.dumps(result, ensure_ascii=False)):
        raise ValueError('instruction-shaped content requires rejection, not publication')
    return result


def auto_publishable(value):
    """No self-assessed confidence, bot use count or repost count can promote a fact."""
    sources = [s for s in value['sources'] if s['verified'] and len(s['excerpt']) >= 20]
    return (not value['risks'] and value['confidence'] == 'high'
            and len({s['origin'] for s in sources}) >= 2
            and len({s['excerpt'] for s in sources}) >= 2)


def material(value):
    """Immutable snapshot given to the curator; no arbitrary file paths."""
    if not isinstance(value, dict):
        raise ValueError('material object required')
    segments = value.get('segments', [])
    if not isinstance(segments, list) or not 1 <= len(segments) <= 120:
        raise ValueError('1..120 evidence segments required')
    url = public_url(value['url'])
    published = stamp(value.get('published_at'))
    origin = text(value.get('origin', value['url']), 120)
    cleaned = [source({**s, 'url': url, 'origin': origin, 'published_at': published,
                       'verified': s.get('kind', 'subtitle') == 'subtitle'}) for s in segments]
    if len({s['id'] for s in cleaned}) != len(cleaned):
        raise ValueError('duplicate evidence identifiers')
    return {'url': url, 'title': text(value.get('title', url), 240), 'origin': origin,
            'published_at': published, 'segments': cleaned, 'hash': digest(cleaned)}
