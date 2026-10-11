"""Opt-in embeddings, public corpus cache and no stored private query text."""
import asyncio
import math
from .models import text, digest


class SemanticSearch:
    def __init__(self, repository, services):
        self.repo, self.services = repository, services
        self.lock = asyncio.Lock()
        self.vectors = {}

    async def search(self, scope, query):
        query = text(query, 1000)
        if not (await asyncio.to_thread(self.repo.semantic_consent, scope))['enabled']:
            return {'ok': True, 'candidates': [], 'consent_required': True}
        if not self.services.embedding_url:
            return {'ok': True, 'candidates': [], 'not_configured': True}
        if self.lock.locked():
            return {'ok': True, 'candidates': [], 'busy': True}
        # Timeout is fail-open to ordinary chat; provider work has its own bounded deadline.
        async with self.lock:
            values, after = [], ''
            for _ in range(2):
                page = await asyncio.to_thread(self.repo.list, 'published', after, 100)
                values.extend(page['entries']); after = page['after']
                if len(page['entries']) < 100:
                    break
            if len(values) > 100:
                raise ValueError('embedding corpus limit is 100; raise only after cost review')
            if not values:
                return {'ok': True, 'candidates': []}
            keys = [digest({'model': self.services.embedding_model, 'text': e['name'] + ': ' + e['meaning']}) for e in values]
            missing = [(key, e['name'] + ': ' + e['meaning']) for key, e in zip(keys, values) if key not in self.vectors]
            await asyncio.to_thread(self.repo.reserve_semantic, len(missing) + 1, self.services.embedding_daily_inputs)
            for offset in range(0, len(missing), 32):
                batch = missing[offset:offset + 32]
                vectors = await self.services.embed([body for _, body in batch])
                self.vectors.update(zip([key for key, _ in batch], vectors))
            vector = (await self.services.embed([query]))[0]
            norm = lambda v: math.sqrt(sum(x * x for x in v))
            candidates = []
            for value, key in zip(values, keys):
                corpus = self.vectors[key]
                if len(vector) != len(corpus):
                    raise ValueError('embedding model dimension changed; clear corpus cache')
                score = sum(a * b for a, b in zip(vector, corpus)) / (norm(vector) * norm(corpus))
                if score >= 0.8:
                    candidates.append({'id': value['id'], 'score': score})
            if len(self.vectors) > 500:
                self.vectors = {key: self.vectors[key] for key in keys}
            return {'ok': True, 'candidates': sorted(candidates, key=lambda v: -v['score'])[:3], 'untrusted': True}
