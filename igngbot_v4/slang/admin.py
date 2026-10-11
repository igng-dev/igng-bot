"""Owner CLI. Credentials stay in environment; imports never overwrite existing versions."""
import argparse
import json
import os
import urllib.request
from pathlib import Path
from .seed import entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def call(action, data):
    secret = os.environ.get('SLANG_ADMIN_SECRET', '')
    if len(secret) < 32:
        raise ValueError('configure SLANG_ADMIN_SECRET outside source control')
    base = os.environ.get('SLANG_API_URL', 'http://127.0.0.1:8792').rstrip('/')
    request = urllib.request.Request(base + '/admin/' + action, data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret})
    with urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({})).open(request, timeout=60) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', help='seed, import, export, or documented owner capability')
    parser.add_argument('--file', type=Path, help='bounded input JSON; export is written to stdout')
    args = parser.parse_args()
    if args.action == 'export':
        result, after = [], ''
        while True:
            page = call('list', {'after': after, 'limit': 200})
            if not page['entries']:
                break
            result.extend(page['entries'])
            after = page['after']
        print(json.dumps({'entries': result}, ensure_ascii=False, indent=2))
        return
    if args.file and args.file.stat().st_size > 512 * 1024:
        raise ValueError('input too large')
    data = json.loads(args.file.read_text()) if args.file else {}
    if args.action in {'seed', 'import'}:
        values = entries() if args.action == 'seed' else data['entries']
        if not isinstance(values, list) or len(values) > 200:
            raise ValueError('import at most 200 candidates per request batch')
        for value in values:
            call('save', {'entry': value, 'status': 'candidate', 'expectedVersion': 0, 'reason': 'owner imported unreviewed starter candidate'})
        print(json.dumps({'imported_candidates': len(values)}))
    else:
        print(json.dumps(call(args.action, data), ensure_ascii=False, default=str, indent=2))


if __name__ == '__main__':
    main()
