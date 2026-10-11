"""Actual official CLI + isolated API, temporary SQL and scripted model wire; restart recovery."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
from uuid import uuid4
import aiohttp
from aiohttp import web
import pymysql
import pytest

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test' or os.getenv('YUNYING_RUN_PROFILE_SMOKE') != '1', reason='temporary database + process opt-in')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_actual_slang_profile_api_evidence_candidate_and_restart_accounting():
    asyncio.run(scenario())


async def scenario():
    root = Path(__file__).resolve().parents[1]
    cache = Path.home() / 'tmp/opencode'
    cache.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(prefix='slang-process-', dir=cache))
    api_port, model_port = port(), port()
    admin, worker = secrets.token_hex(32), secrets.token_hex(32)
    entry_id = 'fixture:' + str(uuid4())
    observed = []
    async def model(request):
        body = await request.json(); observed.append(body)
        assert all(t['name'] in {'slang_read_material', 'slang_lookup', 'slang_submit'} for t in body['tools'])
        name, args = ('slang_read_material', {'after': 0}) if len(observed) == 1 else ('slang_submit', {'candidates': [{
            'id': entry_id, 'name': '合成表达', 'meaning': '本测试材料对游戏失败的自嘲。', 'aliases': ['合成表达'],
            'usage': 'understand_only', 'lifecycle': 'unknown', 'confidence': 'medium', 'evidence_ids': ['segment:0']}]})
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'}); await response.prepare(request)
        async def emit(kind, value):
            await response.write(('event: ' + kind + '\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())
        await emit('message_start', {'type': 'message_start', 'message': {'id': str(uuid4()), 'type': 'message', 'role': 'assistant', 'model': 'fixture', 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}})
        await emit('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': str(uuid4()), 'name': name, 'input': {}}})
        await emit('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args, ensure_ascii=False)}})
        await emit('content_block_stop', {'type': 'content_block_stop', 'index': 0})
        await emit('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 5}})
        await emit('message_stop', {'type': 'message_stop'}); await response.write_eof(); return response
    app = web.Application(); app.router.add_post('/anthropic/v1/messages', model)
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, '127.0.0.1', model_port).start()
    env = {**os.environ, 'DB_HOST': '127.0.0.1', 'DB_PORT': os.getenv('YUNYING_TEST_DB_PORT', '33316'), 'DB_USER': 'root', 'DB_PASSWORD': '', 'DB_NAME': 'yunying_v4_test', 'DB_SSL': '0',
           'SITE_AI_RECORDS_ENABLED': '0', 'SLANG_ADMIN_SECRET': admin, 'SLANG_DSH_SECRET': worker, 'SLANG_PORT': str(api_port), 'SLANG_API_URL': f'http://127.0.0.1:{api_port}',
           'SLANG_CURATOR_ENABLED': '1', 'SLANG_PACKAGE_DIR': str(root / 'slang-curator-dsh'), 'SLANG_PROVIDER': 'deepseek-official', 'SLANG_MODEL': 'deepseek-v4-flash', 'SLANG_DAILY_JOBS': '100', 'SLANG_DAILY_TOKENS': '2000000',
           'DSH_BIN': str(root / 'yunying-dsh/node_modules/.bin/dsh'), 'DSH_HOME': str(data / 'dsh'), 'DSH_TELEMETRY_DISABLED': '1',
           'DEEPSEEK_API_KEY': secrets.token_hex(32), 'DEEPSEEK_BASE_URL': f'http://127.0.0.1:{model_port}/anthropic', 'NO_PROXY': '127.0.0.1,localhost'}
    processes, logs = [], []
    async def spawn(name, command):
        log = open(data / (name + '.log'), 'wb'); logs.append(log)
        proc = await asyncio.create_subprocess_exec(*command, cwd=root, env=env, stdout=log, stderr=log)
        processes.append(proc); return proc
    async def stop(proc):
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
    async def until(check, seconds=120):
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await check():
                    return
            except (aiohttp.ClientError, OSError):
                pass
            await asyncio.sleep(.2)
        raise AssertionError('slang process fixture timed out; logs: ' + str(data))
    def query(sql, values=()):
        with pymysql.connect(host='127.0.0.1', port=int(env['DB_PORT']), user='root', database='yunying_v4_test') as conn:
            with conn.cursor() as cur:
                cur.execute(sql, values); return cur.fetchall()
    try:
        await spawn('api', [sys.executable, '-m', 'igngbot_v4.slang'])
        async with aiohttp.ClientSession() as client:
            async def healthy():
                async with client.get(f'http://127.0.0.1:{api_port}/health') as response:
                    return response.status == 200
            await until(healthy)
            async with client.post(f'http://127.0.0.1:{api_port}/admin/create', headers={'Authorization': 'Bearer ' + admin}, json={
                    'material': {'url': 'https://example.com/' + str(uuid4()), 'title': '合成公开素材', 'origin': 'fixture:original',
                                 'segments': [{'id': 'segment:0', 'start': 0, 'end': 10, 'excerpt': '合成表达在此视频片段中用于对游戏失败的自嘲。这是测试原文，不是生产知识。'}]}}) as response:
                assert response.status == 200, await response.text()
                created = await response.json()
            proc = await spawn('dsh', ['sh', str(root / 'scripts/run-slang-curator-profile.sh')])
            async def completed():
                rows = query('SELECT status,error FROM slang_jobs WHERE id=%s', (created['id'],))
                if rows and rows[0][0] == 'failed':
                    raise AssertionError('curator failed: ' + str(rows[0][1]) + '; logs: ' + str(data))
                return rows and rows[0][0] == 'completed'
            await until(completed)
            await stop(proc)
            await spawn('dsh-restart', ['sh', str(root / 'scripts/run-slang-curator-profile.sh')])
            async def reconciled():
                return query('SELECT status FROM slang_runs WHERE job_id=%s', (created['id'],))[0][0] == 'reconciled'
            await until(reconciled)
        assert len(observed) == 2
        assert query('SELECT COUNT(*) FROM slang_candidates WHERE job_id=%s', (created['id'],))[0][0] == 1
        assert query('SELECT COUNT(*) FROM slang_entries WHERE id=%s', (entry_id,))[0][0] == 0
        assert query('SELECT COUNT(*) FROM slang_calls WHERE job_id=%s', (created['id'],))[0][0] >= 3
    finally:
        for process in processes:
            await stop(process)
        for log in logs:
            log.close()
        await runner.cleanup()
