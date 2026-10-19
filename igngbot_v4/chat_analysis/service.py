"""Internal API, calendar scheduler and snapshot preparation. Never invokes an LLM or runs an Agent loop."""
import asyncio
import hmac
import json
import logging
import os
from datetime import datetime, timezone

import pymysql
from aiohttp import web
from pymysql.cursors import DictCursor
from igngbot_shared.config import Config
from igngbot_v4.ai_records import mirror_native_record
from igngbot_v4.migrate import connect, migrate
from .period import completed_days
from .repository import Repository, Conflict, decode
from .source import QQGroupSource, conversation
from .statistics import compute

logger = logging.getLogger('chat-analysis')


def settings(env=os.environ):
    def number(name, default, low, high):
        value = int(env.get(name, default))
        if not low <= value <= high:
            raise ValueError('invalid chat analysis setting: ' + name)
        return value
    from zoneinfo import ZoneInfo
    zone = env.get('CHAT_ANALYSIS_DEFAULT_TIMEZONE', 'Asia/Shanghai')
    ZoneInfo(zone)
    return {'schedule_enabled': env.get('CHAT_ANALYSIS_SCHEDULE', '1') == '1',
            'default_timezone': zone,
            'provider': env.get('CHAT_ANALYSIS_PROVIDER', 'deepseek-official'),
            'model': env.get('CHAT_ANALYSIS_MODEL', 'deepseek-v4-flash'),
            'prompt_version': 'chat-daily-v1',
            'max_steps': number('CHAT_ANALYSIS_MAX_STEPS', 300, 4, 2048),
            'max_tokens': number('CHAT_ANALYSIS_TOKEN_BUDGET', 3000000, 10000, 20000000),
            'max_seconds': number('CHAT_ANALYSIS_MAX_SECONDS', 2700, 60, 14400),
            'output_tokens': number('CHAT_ANALYSIS_OUTPUT_TOKENS', 8192, 1000, 32768),
            'max_messages': number('CHAT_ANALYSIS_MAX_MESSAGES', 60000, 100, 500000),
            'buffer_seconds': number('CHAT_ANALYSIS_BUFFER_SECONDS', 1800, 0, 21600),
            'catch_up_days': number('CHAT_ANALYSIS_CATCH_UP_DAYS', 3, 1, 14)}


WORKER_READS = {'timeline', 'context', 'search', 'member'}


class Service:
    def __init__(self, repository, sources, admin_secret, worker_secret):
        if len(admin_secret) < 32 or len(worker_secret) < 32 or hmac.compare_digest(admin_secret, worker_secret):
            raise ValueError('chat analysis admin and worker secrets must be strong and distinct')
        self.repo, self.sources = repository, sources
        self.admin_secret, self.worker_secret = admin_secret, worker_secret
        self.background = []

    async def call(self, function, *args, **kwargs):
        # Each repository operation has its own connection; bounded requests don't block the HTTP loop.
        return await asyncio.to_thread(function, *args, **kwargs)

    def source(self, platform):
        source = self.sources.get(platform)
        if source is None:
            raise ValueError('no adapter for this platform')
        return source

    async def prepare_once(self):
        row = await self.call(self.repo.claim_prepare)
        if not row:
            return None
        try:
            source = self.source(row['platform'])
            messages, context, manifest = await self.call(source.snapshot, row['external_id'], str(row['period_start']),
                                                          row['timezone'], row['period_from'], row['period_to'])
            statistics = await self.call(compute, messages, context, manifest, row['period_from'], row['period_to'], row['timezone'])
            return await self.call(self.repo.install_snapshot, row['id'], row['lease_token'], messages, manifest, statistics)
        except Exception as error:
            logger.error('chat analysis snapshot failed: %s', type(error).__name__)
            message = str(error) if isinstance(error, ValueError) else type(error).__name__
            await self.call(self.repo.finish, row['id'], row['lease_token'], False, 'snapshot: ' + message[:200])
            return None

    async def preparation(self):
        while True:
            try:
                while await self.prepare_once():
                    pass
            except Exception as error:
                logger.error('chat analysis preparation deferred: %s', type(error).__name__)
            await asyncio.sleep(15)

    async def admin(self, action, data):
        actor = data.get('actor')
        if not isinstance(actor, str) or not actor.startswith('site:'):
            raise ValueError('auditable site actor required')
        if action == 'create':
            platform, kind, external = conversation(data.get('platform', 'qq'), data.get('conversation_type', 'group'),
                                                    data.get('external_id'))
            if not await self.call(self.source(platform).known, external):
                raise ValueError('conversation has no recorded history')
            configured = {(r['platform'], r['conversation_type'], r['external_id']): r for r in await self.call(self.repo.settings)}
            zone = (configured.get((platform, kind, external)) or {}).get('timezone') or self.repo.config['default_timezone']
            return await self.call(self.repo.create, platform, kind, external, data.get('period_start'), zone, actor,
                                   data.get('request_key'))
        if action == 'retry':
            return await self.call(self.repo.retry, data.get('run_id'), actor, data.get('request_key'))
        if action == 'select':
            return await self.call(self.repo.select, data.get('report_id'), data.get('run_id'), data.get('expected_run_id'))
        if action == 'configure':
            platform, kind, external = conversation(data.get('platform', 'qq'), data.get('conversation_type', 'group'),
                                                    data.get('external_id'))
            if data.get('enabled') is True and not await self.call(self.source(platform).known, external):
                raise ValueError('conversation has no recorded history')
            return await self.call(self.repo.configure, platform, kind, external, data.get('enabled'), data.get('timezone'), actor)
        if action == 'settings':
            return {'ok': True, 'settings': await self.call(self.repo.settings)}
        raise ValueError('unknown management action')

    async def worker(self, action, data):
        if action == 'recovery':
            return await self.call(self.repo.recovery, data.get('run_id'))
        if action == 'reconcile':
            return await self.call(self.repo.account, data.get('run_id'), None, data.get('record', {}), True)
        if action == 'claim':
            return {'ok': True, 'run': await self.call(self.repo.claim)}
        run, token = data.get('run_id'), data.get('lease_token')
        if action == 'heartbeat':
            return await self.call(self.repo.heartbeat, run, token)
        if action == 'phase':
            return await self.call(self.repo.phase, run, token, data.get('phase'))
        if action in WORKER_READS:
            return await self.call(self.repo.events, run, token, data.get('args', {}), action)
        if action == 'previous':
            return await self.call(self.repo.previous, run, token, data.get('args', {}))
        if action in {'observe', 'submit'}:
            return await self.call(self.repo.save, run, token, data.get('args', {}), action == 'submit')
        if action == 'account':
            return await self.call(self.repo.account, run, token, data.get('record', {}))
        if action == 'finish':
            return await self.call(self.repo.finish, run, token, data.get('success') is True, data.get('error'))
        raise ValueError('unknown worker capability')

    async def route(self, request):
        worker = request.path.startswith('/worker/')
        secret = self.worker_secret if worker else self.admin_secret
        if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + secret):
            raise web.HTTPUnauthorized()
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('JSON object required')
            action = request.match_info['action']
            result = await (self.worker(action, data) if worker else self.admin(action, data))
            return web.json_response(result, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))
        except Conflict as error:
            return web.json_response({'error': str(error)[:300]}, status=409)
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response({'error': str(error)[:300]}, status=400)
        except Exception as error:
            logger.error('chat analysis capability failed: %s', type(error).__name__)
            return web.json_response({'error': 'chat analysis temporarily unavailable'}, status=503)

    async def schedule_once(self, now=None):
        created = 0
        for row in await self.call(self.repo.enabled):
            for label in completed_days(row['timezone'], now, self.repo.config['buffer_seconds'], self.repo.config['catch_up_days']):
                # Bounded restart catch-up; unique schedule keys prevent duplicate jobs.
                key = f"schedule:v1:{row['platform']}:{row['conversation_type']}:{row['external_id']}:{label}:{row['timezone']}"
                result = await self.call(self.repo.create, row['platform'], row['conversation_type'], row['external_id'],
                                         label, row['timezone'], 'scheduler', key)
                created += result['status'] == 'pending'
        return created

    async def maintenance(self):
        while True:
            try:
                await self.call(self.repo.expire)
                if self.repo.config.get('schedule_enabled', True):
                    await self.schedule_once(datetime.now(timezone.utc))
            except Exception as error:
                logger.error('chat analysis scheduler deferred: %s', type(error).__name__)
            await asyncio.sleep(60)

    async def mirror(self):
        while True:
            try:
                def pending():
                    with self.repo.transaction() as cur:
                        cur.execute("SELECT a.record_id,a.payload FROM yunying_ai_records a JOIN chat_analysis_calls c ON c.record_id=a.record_id "
                                    "WHERE a.mirror_status='pending' ORDER BY a.dsh_session_id,a.request_seq LIMIT 100")
                        return cur.fetchall()
                for row in await self.call(pending):
                    if await mirror_native_record(row['record_id'], decode(row['payload'])):
                        def acknowledged():
                            with self.repo.transaction() as cur:
                                cur.execute("UPDATE yunying_ai_records SET mirror_status='mirrored' WHERE record_id=%s", (row['record_id'],))
                        await self.call(acknowledged)
            except Exception as error:
                logger.error('chat analysis AI export deferred: %s', type(error).__name__)
            await asyncio.sleep(5)

    def app(self):
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post('/admin/{action}', self.route)
        app.router.add_post('/worker/{action}', self.route)

        async def health(_request):
            try:
                def probe():
                    with self.repo.transaction() as cur:
                        cur.execute('SELECT 1 FROM chat_analysis_reports LIMIT 1')
                await self.call(probe)
                return web.json_response({'ok': True})
            except Exception:
                return web.json_response({'ok': False}, status=503)
        app.router.add_get('/health', health)

        async def start(_app):
            self.background = [asyncio.create_task(self.maintenance()), asyncio.create_task(self.preparation()),
                               asyncio.create_task(self.mirror())]

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
    config = settings()
    # The same checksum-managed additive migrations as V4, never an ad hoc production schema push.
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()

    def read_connect():
        # Snapshot reads can legitimately exceed the 10 s control-plane timeout on busy groups.
        c = Config()
        return pymysql.connect(host=c.DB_HOST, port=c.DB_PORT, user=c.DB_USER, password=c.DB_PASSWORD, database=c.DB_NAME,
                               charset='utf8mb4', autocommit=True, cursorclass=DictCursor, ssl=Config.db_ssl_context(),
                               connect_timeout=10, read_timeout=120, init_command="SET time_zone = '+00:00'")
    sources = {'qq': QQGroupSource(read_connect, Config.BOT_USER_ID or None, config['max_messages'])}
    service = Service(Repository(connect, config), sources,
                      os.environ.get('CHAT_ANALYSIS_ADMIN_SECRET', ''), os.environ.get('CHAT_ANALYSIS_DSH_SECRET', ''))
    web.run_app(service.app(), host=os.getenv('CHAT_ANALYSIS_HOST', '127.0.0.1'), port=int(os.getenv('CHAT_ANALYSIS_PORT', '8792')))
