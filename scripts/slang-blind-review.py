"""Create reproducible anonymous A/B/C packets from authorized, pre-generated replies.

Input: [{"case_id":"...", "mode":"baseline|recognition|full", "reply":"..."}].
No model requests or private data collection. Output goes to stdout, not the repository.
Use --key to emit the separate answer key AFTER reviewers return their ratings.
"""
import argparse
import json
import random
from pathlib import Path


def packets(rows, seed):
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError('bounded response list required')
    cases = {}
    for row in rows:
        if row['mode'] not in {'baseline', 'recognition', 'full'} or len(row['reply']) > 4000:
            raise ValueError('invalid comparison response')
        mode = cases.setdefault(row['case_id'], {})
        if row['mode'] in mode:
            raise ValueError('duplicate comparison arm')
        mode[row['mode']] = row['reply']
    rng, result, key = random.Random(seed), [], []
    for id, replies in sorted(cases.items()):
        if len(replies) != 3:
            raise ValueError('each case requires all three arms')
        arms = list(replies); rng.shuffle(arms)
        for label, arm in zip('ABC', arms):
            token = f'{id}:{label}'
            result.append({'review_id': token, 'reply': replies[arm], 'ratings': {'understanding': None, 'naturalness': None, 'forced_memes': None, 'stale_expression': None}, 'comment': ''})
            key.append({'review_id': token, 'mode': arm})
    return result, key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--seed', type=int, default=20261012)
    parser.add_argument('--key', action='store_true')
    args = parser.parse_args()
    if args.input.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('input too large')
    result, key = packets(json.loads(args.input.read_text()), args.seed)
    print(json.dumps({'warning': 'human ratings pending; synthetic fixture results are not real chat acceptance', 'items': key if args.key else result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
