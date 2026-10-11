"""Isolated owner/worker API and scheduler. Never runs an LLM loop or accesses QQ history."""
import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
from aiohttp import web
from igngbot_v4.migrate import connect, migrate
from .repository import Repository, Conflict
from .sources import SourceReader
from .media import ModelServices
from .semantic import SemanticSearch
from igngbot_v4.ai_records import mirror_native_record
from .repository import decode

logger = logging.getLogger('slang')


def settings(env=os.environ):
    def number(name, default, low, high):
        value = int(env.get(name, default))
        if not low <= value <= high:
            raise ValueError('invalid slang budget: ' + name)
        return value
    return {'provider': env.get('SLANG_PROVIDER', 'deepseek-official'), 'model': env.get('SLANG_MODEL', 'deepseek-v4-flash'),
            'max_steps': number('SLANG_MAX_STEPS', 24, 2, 100), 'max_tokens': number('SLANG_TOKEN_BUDGET', 30000, 1000, 200000),
            'max_seconds': number('SLANG_MAX_SECONDS', 600, 30, 3600), 'output_tokens': number('SLANG_OUTPUT_TOKENS', 4096, 256, 16000),
            'daily_jobs': number('SLANG_DAILY_JOBS', 10, 1, 100), 'daily_tokens': number('SLANG_DAILY_TOKENS', 150000, 1000, 2000000)}


class Service:
    def __init__(self, repository, reader, admin_secret, worker_secret, read_secret='', model_services=None):
        if len(admin_secret) < 32 or len(worker_secret) < 32 or hmac.compare_digest(admin_secret, worker_secret):
            raise ValueError('slang admin and worker secrets must be strong and distinct')
        self.repo, self.reader = repository, reader
        self.admin_secret, self.worker_secret = admin_secret, worker_secret
        if read_secret and (len(read_secret) < 32 or read_secret in {admin_secret, worker_secret}):
            raise ValueError('reader secret must be strong and separate')
        self.read_secret = read_secret
        self.semantic = SemanticSearch(repository, model_services) if model_services else None
        if model_services:
            model_services.accounting = repository
        self.poll_lock = asyncio.Lock()
        self.background = []

    async def call(self, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, **kwargs)

    async def poll(self, id=None):
        async with self.poll_lock:
            result = await self.call(self.repo.sources, id is None)
            outcomes = []
            for row in result['sources']:
                config = row['config']
                if not config['enabled'] or (id and row['id'] != id):
                    continue
                token = await self.call(self.repo.source_claim, row['id'])
                if not token:
                    continue
                try:
                    async with asyncio.timeout(600):
                        items = await self.reader.discover(config)
                        count = 0
                        for item in items:
                            try:
                                if await self.call(self.repo.known_url, row['id'], item['url']):
                                    continue
                                snapshot = await self.reader.snapshot(config, item)
                                created = await self.call(self.repo.create, row['id'], snapshot)
                                count += not created.get('unchanged', False)
                            except (ValueError, Conflict) as error:
                                # Safe class only; signed media URLs or provider bodies never reach logs.
                                outcomes.append({'source': row['id'], 'error': type(error).__name__})
                    await self.call(self.repo.source_polled, row['id'], config['poll_seconds'])
                    outcomes.append({'source': row['id'], 'created': count})
                except Exception as error:
                    await self.call(self.repo.source_polled, row['id'], config['poll_seconds'], type(error).__name__)
                    outcomes.append({'source': row['id'], 'error': type(error).__name__})
                finally:
                    await self.call(self.repo.source_release, row['id'], token)
            return {'ok': True, 'outcomes': outcomes}

    async def route(self, request):
        worker = request.path.startswith('/worker/')
        reader = request.path.startswith('/reader/')
        secret = self.read_secret if reader else self.worker_secret if worker else self.admin_secret
        supplied = request.headers.get('Authorization', '')
        if not secret or not supplied.isascii() or not hmac.compare_digest(supplied, 'Bearer ' + secret):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('JSON object required')
            action = request.match_info['action']
            if reader:
                if action != 'semantic' or not self.semantic:
                    raise ValueError('reader capability unavailable')
                async with asyncio.timeout(2):
                    result = await self.semantic.search(data['scope'], data['query'])
            elif not worker:
                # Token holders are Owner operators; provenance is auditable but not a new grant.
                actor = data.get('actor', 'owner-api')
                if action == 'list':
                    result = await self.call(self.repo.list, data.get('status'), data.get('after', ''), data.get('limit', 100))
                elif action == 'save':
                    result = await self.call(self.repo.save, data['entry'], data.get('status', 'candidate'), data['expectedVersion'], actor, data['reason'])
                elif action == 'versions':
                    result = await self.call(self.repo.versions, data['id'])
                elif action == 'rollback':
                    result = await self.call(self.repo.rollback, data['id'], data['version'], data['expectedVersion'], actor, data['reason'])
                elif action == 'candidates':
                    result = await self.call(self.repo.candidates)
                elif action == 'review':
                    result = await self.call(self.repo.review, data['id'], data.get('expectedVersion', 0), actor, data['reason'], data.get('publish', True) is True)
                elif action == 'policy':
                    result = await self.call(self.repo.set_policy, data['policy']) if 'policy' in data else await self.call(self.repo.policy)
                elif action == 'source-save':
                    result = await self.call(self.repo.source_save, data['source'])
                elif action == 'sources':
                    result = await self.call(self.repo.sources)
                elif action == 'poll':
                    result = await self.poll(data.get('id'))
                elif action == 'create':
                    result = await self.call(self.repo.create, data.get('source_id', 'owner-import'), data['material'])
                elif action == 'jobs':
                    result = await self.call(self.repo.jobs)
                elif action == 'retry':
                    result = await self.call(self.repo.retry, data['id'])
                elif action == 'feedback':
                    result = await self.call(self.repo.feedback, data['scope'], data['id'], data['preference'])
                elif action == 'consent':
                    result = await self.call(self.repo.consent, data['scope'], data['enabled'])
                elif action == 'semantic-consent':
                    result = await self.call(self.repo.semantic_consent, data['scope'], data['enabled'])
                elif action == 'metrics':
                    result = await self.call(self.repo.metrics)
                elif action == 'recovery-list':
                    result = await self.call(self.repo.recovery)
                elif action == 'resolve-unavailable-session':
                    if data.get('acknowledge_unknown_usage') is not True:
                        raise ValueError('explicit accounting-loss acknowledgement required')
                    result = await self.call(self.repo.resolve_unavailable_session, data['session'], actor, data['reason'])
                else:
                    raise ValueError('unknown owner action')
            elif action == 'claim':
                await self.call(self.repo.expire)
                result = await self.call(self.repo.claim)
            elif action == 'recovery':
                await self.call(self.repo.expire)
                result = await self.call(self.repo.recovery, data.get('session'))
            elif action == 'reconcile':
                result = await self.call(self.repo.account, data['run_id'], None, data['record'], data['session'])
            else:
                id, token = data['run_id'], data['lease_token']
                args = data.get('args', {})
                if action == 'heartbeat':
                    result = await self.call(self.repo.heartbeat, id, token)
                elif action == 'evidence':
                    result = await self.call(self.repo.evidence, id, token, args.get('after', 0), args.get('limit', 12))
                elif action == 'lookup':
                    await self.call(self.repo.heartbeat, id, token)
                    result = await self.call(self.repo.lookup, args['query'])
                elif action == 'submit':
                    result = await self.call(self.repo.submit, id, token, args['candidates'])
                elif action == 'account':
                    result = await self.call(self.repo.account, id, token, data['record'])
                elif action == 'finish':
                    result = await self.call(self.repo.finish, id, token, data.get('success'), data.get('error'))
                else:
                    raise ValueError('unknown worker capability')
            return web.json_response(result, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))
        except Conflict as error:
            return web.json_response({'error': str(error)}, status=409)
        except (ValueError, KeyError, TypeError):
            return web.json_response({'error': 'invalid slang data or evidence; inspect the documented contract'}, status=400)
        except Exception as error:
            logger.error('slang capability failed: %s', type(error).__name__)
            return web.json_response({'error': 'slang temporarily unavailable'}, status=503)

    async def maintenance(self):
        while True:
            try:
                await self.call(self.repo.expire)
                policy = (await self.call(self.repo.policy))['policy']
                if policy.get('maintenance'):
                    await self.poll()
                if policy.get('auto_publish'):
                    await self.call(self.repo.auto_publish)
            except Exception as error:
                logger.warning('slang maintenance deferred: %s', type(error).__name__)
            await asyncio.sleep(60)

    async def mirror(self):
        while True:
            try:
                def pending():
                    with self.repo.transaction() as cur:
                        cur.execute("SELECT a.record_id,a.payload FROM yunying_ai_records a JOIN slang_calls s ON s.record_id=a.record_id WHERE a.mirror_status='pending' ORDER BY a.dsh_session_id,a.request_seq LIMIT 100")
                        return cur.fetchall()
                for row in await self.call(pending):
                    if await mirror_native_record(row['record_id'], decode(row['payload'])):
                        def acknowledged():
                            with self.repo.transaction() as cur:
                                cur.execute("UPDATE yunying_ai_records SET mirror_status='mirrored' WHERE record_id=%s", (row['record_id'],))
                        await self.call(acknowledged)
            except Exception as error:
                logger.warning('slang accounting mirror deferred: %s', type(error).__name__)
            await asyncio.sleep(5)

    def app(self):
        app = web.Application(client_max_size=512 * 1024)
        app.router.add_post('/admin/{action}', self.route)
        app.router.add_post('/worker/{action}', self.route)
        app.router.add_post('/reader/{action}', self.route)
        async def health(_request):
            try:
                await self.call(self.repo.policy)
                return web.json_response({'ok': True})
            except Exception:
                return web.json_response({'ok': False}, status=503)
        async def page(_request):
            return web.Response(text=Path(__file__).with_name('manage.html').read_text(), content_type='text/html',
                                headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                                         'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        app.router.add_get('/health', health)
        app.router.add_get('/manage', page)
        async def asset(request):
            name = request.match_info['name']
            if name not in {'manage.js', 'manage.css'}:
                raise web.HTTPNotFound()
            return web.Response(text=Path(__file__).with_name(name).read_text(), content_type='text/javascript' if name.endswith('.js') else 'text/css', headers={'Cache-Control': 'no-store'})
        app.router.add_get('/assets/{name}', asset)
        async def start(_app):
            self.background = [asyncio.create_task(self.maintenance()), asyncio.create_task(self.mirror())]
        async def stop(_app):
            for task in self.background:
                task.cancel()
            await asyncio.gather(*self.background, return_exceptions=True)
            from igngbot_shared.call_log_db import close_call_log_pool
            await close_call_log_pool()
        app.on_startup.append(start)
        app.on_cleanup.append(stop)
        return app


def main():
    logging.basicConfig(level=logging.INFO)
    with connect() as conn:
        migrate(conn)
    models = ModelServices()
    service = Service(Repository(connect, settings()), SourceReader(media=models),
                      os.getenv('SLANG_ADMIN_SECRET', ''), os.getenv('SLANG_DSH_SECRET', ''),
                      os.getenv('SLANG_READ_SECRET', ''), models)
    web.run_app(service.app(), host=os.getenv('SLANG_HOST', '127.0.0.1'), port=int(os.getenv('SLANG_PORT', '8792')),
                access_log=None)
