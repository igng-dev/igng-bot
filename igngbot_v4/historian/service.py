"""Internal API + calendar scheduler. Does not invoke an LLM or run an Agent loop."""
import asyncio
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pymysql
from aiohttp import web
from pymysql.cursors import DictCursor
from igngbot_shared.config import Config
from igngbot_v4.migrate import connect, migrate
from igngbot_v4.ai_records import mirror_native_record
from .events import MCSource
from .period import completed_periods
from .repository import Repository, Conflict, decode
from .statistics import compute

logger = logging.getLogger('historian')


def settings(env=os.environ):
    servers = json.loads(env.get('HISTORIAN_SERVERS', '{}'))
    if not isinstance(servers, dict) or not servers:
        raise ValueError('HISTORIAN_SERVERS must map configured server IDs to IANA zones')
    zones = {int(key): str(value) for key, value in servers.items()}
    from zoneinfo import ZoneInfo
    for server, zone in zones.items():
        if server <= 0:
            raise ValueError('invalid server id')
        ZoneInfo(zone)
    return {'servers': list(zones), 'zones': zones,
            'schedule_enabled': env.get('HISTORIAN_SCHEDULE', '1') == '1',
            'provider': env.get('HISTORIAN_PROVIDER', 'deepseek-official'),
            'model': env.get('HISTORIAN_MODEL', 'deepseek-v4-flash'),
            'prompt_version': 'server-historian-v1',
            'max_steps': min(2048, max(1, int(env.get('HISTORIAN_MAX_STEPS', '512')))),
            'max_tokens': min(10000000, max(1000, int(env.get('HISTORIAN_TOKEN_BUDGET', '2000000')))),
            'max_seconds': min(14400, max(60, int(env.get('HISTORIAN_MAX_SECONDS', '3600')))),
            'output_tokens': min(32768, max(1000, int(env.get('HISTORIAN_OUTPUT_TOKENS', '8192'))))}


def mc_connect():
    return pymysql.connect(host=Config.MC_DB_HOST, port=Config.MC_DB_PORT, user=Config.MC_DB_USER,
                           password=Config.MC_DB_PASSWORD, database=Config.MC_DB_NAME,
                           charset='utf8mb4', cursorclass=DictCursor, autocommit=True,
                           ssl=Config.db_ssl_context(), connect_timeout=10, read_timeout=60,
                           init_command="SET time_zone='+00:00'")


class Service:
    def __init__(self, repository, source, admin_secret, worker_secret):
        if len(admin_secret) < 32 or len(worker_secret) < 32 or hmac.compare_digest(admin_secret, worker_secret):
            raise ValueError('historian admin and worker secrets must be strong and distinct')
        self.repo, self.source = repository, source
        self.admin_secret, self.worker_secret = admin_secret, worker_secret
        self.lock = asyncio.Lock()
        self.background = []

    async def call(self, function, *args, **kwargs):
        # Each repository operation has its own connection; bounded requests don't block the HTTP loop.
        return await asyncio.to_thread(function, *args, **kwargs)

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
            if not worker:
                actor = data.get('actor')
                if not isinstance(actor, str) or not actor.startswith('site:'):
                    raise ValueError('auditable site actor required')
                if action == 'create':
                    server = data.get('server_id')
                    if not isinstance(server, int) or isinstance(server, bool) or server not in self.repo.config['servers']:
                        raise ValueError('server outside historian allowlist')
                    result = await self.call(self.repo.create, server, data.get('kind'), data.get('period_start'),
                                             self.repo.config['zones'][server], actor, data.get('request_key'))
                elif action == 'retry':
                    result = await self.call(self.repo.retry, data.get('run_id'), actor, data.get('request_key'))
                elif action == 'select':
                    result = await self.call(self.repo.select, data.get('report_id'), data.get('run_id'), data.get('expected_run_id'))
                else:
                    raise ValueError('unknown management action')
            elif action == 'recovery':
                result = await self.call(self.repo.recovery, data.get('run_id'))
            elif action == 'reconcile':
                result = await self.call(self.repo.account, data.get('run_id'), None, data.get('record', {}), True)
            elif action == 'claim':
                # Snapshot preparation is serialized separately from the long model execution.
                async with self.lock:
                    row = await self.call(self.repo.claim)
                    if row:
                        try:
                            events, history, manifest = await self.call(self.source.snapshot, row['server_id'], row['period_from'], row['period_to'])
                            statistics = await self.call(compute, events, history, row['period_from'], row['period_to'], row['timezone'])
                            await self.call(self.repo.snapshot, row['id'], row['lease_token'], events, manifest, statistics)
                        except Exception as error:
                            await self.call(self.repo.finish, row['id'], row['lease_token'], False, type(error).__name__)
                            raise
                        row = {k: row[k] for k in ('id', 'report_id', 'server_id', 'kind', 'timezone', 'period_start',
                                                   'period_from', 'period_to', 'dsh_session_id', 'lease_token', 'config')}
                    result = {'ok': True, 'run': row}
            else:
                run, token = data.get('run_id'), data.get('lease_token')
                if action == 'heartbeat':
                    result = await self.call(self.repo.heartbeat, run, token)
                elif action == 'phase':
                    result = await self.call(self.repo.phase, run, token, data.get('phase'))
                elif action in {'timeline', 'context', 'search', 'player'}:
                    result = await self.call(self.repo.events, run, token, data.get('args', {}), action)
                elif action == 'previous':
                    result = await self.call(self.repo.previous, run, token, data.get('args',{}))
                elif action in {'observe', 'submit'}:
                    result = await self.call(self.repo.save, run, token, data.get('args', {}), action == 'submit')
                elif action == 'account':
                    result = await self.call(self.repo.account, run, token, data.get('record', {}))
                elif action == 'finish':
                    result = await self.call(self.repo.finish, run, token, data.get('success') is True, data.get('error'))
                else:
                    raise ValueError('unknown worker capability')
            return web.json_response(result, dumps=lambda value: json.dumps(value, ensure_ascii=False, default=str))
        except Conflict as error:
            return web.json_response({'error': str(error)}, status=409)
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response({'error': str(error)[:200]}, status=400)
        except Exception as error:
            logger.error('historian capability failed: %s', type(error).__name__)
            return web.json_response({'error': 'historian temporarily unavailable'}, status=503)

    async def maintenance(self):
        while True:
            try:
                await self.call(self.repo.expire)
                if not self.repo.config.get('schedule_enabled', True):
                    await asyncio.sleep(60)
                    continue
                now = datetime.now(timezone.utc)
                for server, zone in self.repo.config['zones'].items():
                    # Bounded restart catch-up; unique schedule keys prevent duplicate jobs.
                    for days in range(14):
                        for kind, label in completed_periods(zone, now - timedelta(days=days)):
                            key = f'schedule:v1:{server}:{kind}:{label}:{zone}'
                            await self.call(self.repo.create, server, kind, label, zone, 'scheduler', key)
            except Exception as error:
                logger.error('historian scheduler deferred: %s', type(error).__name__)
            await asyncio.sleep(60)

    async def mirror(self):
        while True:
            try:
                def pending():
                    with self.repo.transaction() as cur:
                        cur.execute("SELECT a.record_id,a.payload FROM yunying_ai_records a JOIN historian_calls h ON h.record_id=a.record_id "
                                    "WHERE a.mirror_status='pending' ORDER BY a.dsh_session_id,a.request_seq LIMIT 100")
                        return cur.fetchall()
                for row in await self.call(pending):
                    if await mirror_native_record(row['record_id'], decode(row['payload'])):
                        def acknowledged():
                            with self.repo.transaction() as cur:
                                cur.execute("UPDATE yunying_ai_records SET mirror_status='mirrored' WHERE record_id=%s", (row['record_id'],))
                        await self.call(acknowledged)
            except Exception as error:
                logger.error('historian AI export deferred: %s', type(error).__name__)
            await asyncio.sleep(5)

    def app(self):
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post('/admin/{action}', self.route)
        app.router.add_post('/worker/{action}', self.route)
        async def health(_request):
            try:
                def probe():
                    with self.repo.transaction() as cur:
                        cur.execute('SELECT 1 FROM historian_reports LIMIT 1')
                await self.call(probe)
                return web.json_response({'ok': True})
            except Exception:
                return web.json_response({'ok': False}, status=503)
        app.router.add_get('/health', health)
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
    config = settings()
    # The same checksum-managed additive migrations as V4, never an ad hoc production schema push.
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()
    tables = {}
    if os.getenv('MC_DATABASE_MODE', 'unified') == 'split':
        tables = {'chat_messages': os.getenv('MC_CHAT_DB', 'mc_chatlogs'),
                  'player_event_logs': os.getenv('MC_ACCOUNT_DB', 'mc_account')}
    service = Service(Repository(connect, config), MCSource(mc_connect, tables=tables),
                      os.environ.get('HISTORIAN_ADMIN_SECRET', ''), os.environ.get('HISTORIAN_DSH_SECRET', ''))
    web.run_app(service.app(), host=os.getenv('HISTORIAN_HOST', '127.0.0.1'), port=int(os.getenv('HISTORIAN_PORT', '8791')))
