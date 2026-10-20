"""Independent Python API + official chat analyst CLI, real SQL and a synthetic model wire."""
import asyncio
from datetime import datetime, timedelta, timezone
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

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test' or os.getenv('YUNYING_RUN_PROFILE_SMOKE') != '1',
                                reason='disposable database + process opt-in')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_independent_published_chat_analyst_profile_and_api():
    asyncio.run(scenario())


async def scenario():
    root = Path(__file__).resolve().parents[1]
    data = Path(tempfile.mkdtemp(prefix='chat-analysis-process-'))
    api_port, model_port = port(), port()
    admin, worker = secrets.token_hex(32), secrets.token_hex(32)
    group = 1600000000 + uuid4().int % 100000000
    label = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    start = datetime.fromisoformat(label) - timedelta(hours=8)
    db_port = int(os.getenv('YUNYING_TEST_DB_PORT', '33316'))
    conn = pymysql.connect(host='127.0.0.1', port=db_port, user='root', database='yunying_v4_test', autocommit=True)
    refs = []
    with conn.cursor() as cur:
        cur.execute('INSERT INTO group_configs(group_id,group_name) VALUES(%s,%s)', (group, '合成群'))
        for n, (sender, name, text) in enumerate([(51234567, '阿青', '今晚开黑吗'), (57654321, '小白', '来！')]):
            cur.execute('INSERT INTO message_logs(group_id,sender_id,sender_name,message_content,plain_text_content,message_structure,msg_id,created_at)'
                        ' VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', (group, sender, name, text, text, '[]', f'p-{n}', start + timedelta(hours=2, minutes=n)))
            refs.append(f'qq:{cur.lastrowid}')
    conn.close()
    report = {'headline': '阿青约开黑，小白答应',
              'highlights': [{'text': '阿青约今晚开黑，小白答应', 'evidence_refs': refs}],
              'topics': [{'title': '开黑', 'gist': '阿青约开黑，小白答应。', 'points': [{'text': '阿青问今晚开不开黑', 'evidence_refs': refs}],
                          'outcome': {'status': 'settled', 'text': '一起开黑'}, 'participants': ['u1', 'u2'], 'certainty': 'fact'}],
              'quotes': [{'ref': refs[1], 'context': '小白在回应阿青'}],
              'titles': [{'member': 'u1', 'title': '组局发起人', 'reason': '先喊人', 'evidence_refs': [refs[0]]}],
              'open_items': [], 'slang': []}
    observed = []

    async def model(request):
        body = await request.json()
        observed.append(body)
        names = [t['name'] for t in body.get('tools', [])]
        assert names and all(name == 'skill' or name.startswith('chat_') for name in names), names
        wire = json.dumps(body, ensure_ascii=False)
        assert '51234567' not in wire and 'top_members' not in wire, 'account IDs or statistics leaked to the model'
        name, args = ('chat_timeline', {'after': 0}) if len(observed) == 1 else ('chat_submit', {'report': report})
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)

        async def emit(kind, value):
            await response.write(('event: ' + kind + '\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())
        await emit('message_start', {'type': 'message_start', 'message': {'id': str(uuid4()), 'type': 'message', 'role': 'assistant',
                   'model': 'fixture', 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}})
        await emit('content_block_start', {'type': 'content_block_start', 'index': 0,
                   'content_block': {'type': 'tool_use', 'id': str(uuid4()), 'name': name, 'input': {}}})
        await emit('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                   'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args, ensure_ascii=False)}})
        await emit('content_block_stop', {'type': 'content_block_stop', 'index': 0})
        await emit('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 5}})
        await emit('message_stop', {'type': 'message_stop'})
        await response.write_eof()
        return response
    app = web.Application()
    app.router.add_post('/anthropic/v1/messages', model)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', model_port).start()
    env = {**os.environ, 'DB_HOST': '127.0.0.1', 'DB_PORT': str(db_port), 'DB_USER': 'root', 'DB_PASSWORD': '', 'DB_NAME': 'yunying_v4_test',
           'SITE_AI_RECORDS_ENABLED': '0', 'CHAT_ANALYSIS_SCHEDULE': '0', 'BOT_USER_ID': '3999999999',
           'CHAT_ANALYSIS_ADMIN_SECRET': admin, 'CHAT_ANALYSIS_DSH_SECRET': worker, 'CHAT_ANALYSIS_PORT': str(api_port),
           'CHAT_ANALYSIS_API_URL': f'http://127.0.0.1:{api_port}', 'CHAT_ANALYSIS_PROVIDER': 'deepseek-official',
           'CHAT_ANALYSIS_MODEL': 'deepseek-v4-flash', 'CHAT_ANALYST_PACKAGE_DIR': str(root / 'chat-analyst-dsh'),
           'DSH_BIN': str(root / 'yunying-dsh/node_modules/.bin/dsh'), 'DSH_HOME': str(data / 'dsh'), 'DSH_TELEMETRY_DISABLED': '1',
           'DEEPSEEK_API_KEY': secrets.token_hex(32), 'DEEPSEEK_BASE_URL': f'http://127.0.0.1:{model_port}/anthropic',
           'NO_PROXY': '127.0.0.1,localhost'}
    processes, logs = [], []

    async def spawn(name, command):
        log = open(data / (name + '.log'), 'wb')
        logs.append(log)
        processes.append(await asyncio.create_subprocess_exec(*command, cwd=root, env=env, stdout=log, stderr=log))

    async def wait_until(check, seconds=120):
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            if any(p.returncode is not None for p in processes):
                raise AssertionError(f'process exited; fixture logs: {data}')
            try:
                if await check():
                    return
            except (aiohttp.ClientError, OSError):
                pass
            await asyncio.sleep(.2)
        raise AssertionError(f'fixture timed out; logs: {data}')

    def run_row(run_id):
        db = pymysql.connect(host='127.0.0.1', port=db_port, user='root', database='yunying_v4_test')
        try:
            with db.cursor() as cur:
                cur.execute('SELECT status,error,markdown FROM chat_analysis_runs WHERE id=%s', (run_id,))
                return cur.fetchone()
        finally:
            db.close()
    try:
        await spawn('api', [sys.executable, '-m', 'igngbot_v4.chat_analysis'])
        async with aiohttp.ClientSession() as client:
            async def healthy():
                async with client.get(f'http://127.0.0.1:{api_port}/health') as response:
                    return response.status == 200
            await wait_until(healthy)
            headers = {'Authorization': 'Bearer ' + admin}
            async with client.post(f'http://127.0.0.1:{api_port}/admin/configure', headers=headers, json={
                    'platform': 'qq', 'conversation_type': 'group', 'external_id': str(group), 'enabled': True, 'actor': 'site:fixture'}) as response:
                assert response.status == 200, await response.text()
            async with client.post(f'http://127.0.0.1:{api_port}/admin/create', headers={'Authorization': 'Bearer ' + worker}, json={}) as response:
                assert response.status == 401
            async with client.post(f'http://127.0.0.1:{api_port}/admin/create', headers=headers, json={
                    'platform': 'qq', 'conversation_type': 'group', 'external_id': str(group), 'period_start': label,
                    'actor': 'site:fixture', 'request_key': str(uuid4())}) as response:
                assert response.status == 200, await response.text()
                created = await response.json()

        async def ready():
            status, error, _ = run_row(created['run_id'])
            if status == 'failed':
                raise AssertionError(f'snapshot failed: {error}')
            return status == 'ready'
        await wait_until(ready, 60)
        assert observed == [], 'snapshot preparation must not call the model'
        await spawn('dsh', ['sh', str(root / 'scripts/run-chat-analyst-profile.sh')])

        async def completed():
            status, error, _ = run_row(created['run_id'])
            if status == 'failed':
                raise AssertionError(f'analysis failed: {error}; logs: {data}')
            return status == 'completed'
        await wait_until(completed, 180)
        assert len(observed) == 2
        status, error, markdown = run_row(created['run_id'])
        assert '小白' in markdown and '组局发起人' in markdown
        db = pymysql.connect(host='127.0.0.1', port=db_port, user='root', database='yunying_v4_test')
        try:
            with db.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM chat_analysis_calls WHERE run_id=%s', (created['run_id'],))
                assert cur.fetchone()[0] >= 3
                cur.execute('SELECT current_run_id FROM chat_analysis_reports WHERE id=%s', (created['report_id'],))
                assert cur.fetchone()[0] == created['run_id']
        finally:
            db.close()
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), 15)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for log in logs:
            log.close()
        await runner.cleanup()
