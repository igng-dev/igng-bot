"""Official moderation Profile + wire fixture; never connects to QQ/SQL/paid models."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import tempfile
from uuid import uuid4

from aiohttp import web
import pytest


@pytest.mark.skipif(os.getenv('YUNYING_RUN_PROFILE_SMOKE') != '1', reason='explicit native CLI smoke opt-in')
def test_moderation_profile_wire_and_persistent_session():
    asyncio.run(scenario())


async def scenario():
    root = Path(__file__).resolve().parents[1]
    session_id = str(uuid4())
    secret = secrets.token_hex(32)
    completed = asyncio.Event()
    calls, requests, errors = [], [], []
    phase = {'task': None, 'claimed': False, 'step': 0}
    process = None

    async def capability(request):
        assert request.headers.get('Authorization') == 'Bearer ' + secret
        action = request.match_info['action']
        data = await request.json()
        calls.append((action, data))
        if action == 'claim':
            task = None if phase['claimed'] else phase['task']
            phase['claimed'] = True
            return web.json_response({'task': task})
        assert all(data[key] == phase['task'][key] for key in ['taskId', 'runId', 'leaseToken'])
        if action == 'manifest':
            return web.json_response({'units': [{'id': 'body:0', 'kind': 'TEXT', 'hash': 'b' * 64}], 'coverage': []})
        if action == 'read':
            return web.json_response({'id': 'body:0', 'kind': 'TEXT', 'text': '合成固定作品正文', 'hash': 'b' * 64})
        if action == 'submit':
            completed.set()
        if action == 'defer':
            errors.append(data)
            completed.set()
        return web.json_response({'ok': True, 'stop': False, 'status': 'APPLIED'})

    async def messages(request):
        body = await request.json()
        requests.append(body)
        names = [tool['name'] for tool in body.get('tools', [])]
        if not names or any(name != 'skill' and not name.startswith('work_') for name in names):
            errors.append({'exposed_tools': names})
        step = phase['step']
        phase['step'] += 1
        tool, args = [('work_manifest', {}), ('work_read', {'unitId': 'body:0'}),
                      ('work_submit', {'result': {'verdict': 'APPROVED', 'snapshotHash': 'a' * 64, 'policyVersion': 'works-v1'}})][min(step, 2)]
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)

        async def emit(name, value):
            await response.write(('event: ' + name + '\ndata: ' + json.dumps(value, ensure_ascii=False) + '\n\n').encode())

        await emit('message_start', {'type': 'message_start', 'message': {'id': str(uuid4()), 'type': 'message', 'role': 'assistant',
                   'model': 'deepseek-v4-flash', 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 0}}})
        await emit('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': str(uuid4()), 'name': tool, 'input': {}}})
        await emit('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args)}})
        await emit('content_block_stop', {'type': 'content_block_stop', 'index': 0})
        await emit('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 5}})
        await emit('message_stop', {'type': 'message_stop'})
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post('/api/internal/work-moderation/{action}', capability)
    app.router.add_post('/anthropic/v1/messages', messages)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='moderation-profile-') as data:
        env = {**os.environ, 'DSH_HOME': str(Path(data) / 'dsh'), 'DSH_TELEMETRY_DISABLED': '1',
               'MODERATION_PACKAGE_DIR': str(root / 'moderation-dsh'), 'DSH_BIN': str(root / 'yunying-dsh/node_modules/.bin/dsh'),
               'WORK_MODERATION_WORKER_ENABLED': '1', 'WORK_MODERATION_SITE_URL': f'http://127.0.0.1:{port}',
               'WORK_MODERATION_INTERNAL_SECRET': secret, 'WORK_MODERATION_MODEL_PROVIDER': 'work-moderation',
               'WORK_MODERATION_MODEL': 'deepseek-v4-flash', 'WORK_MODERATION_CONCURRENCY': '1',
               'WORK_MODERATION_MODEL_API_KEY': secrets.token_hex(32), 'WORK_MODERATION_MODEL_BASE_URL': f'http://127.0.0.1:{port}/anthropic',
               'NO_PROXY': '127.0.0.1,localhost'}
        try:
            for iteration in range(2):
                phase.update(task={'protocolVersion': 1, 'taskId': str(uuid4()), 'runId': str(uuid4()), 'messageId': str(uuid4()),
                                  'sessionId': session_id, 'sessionState': 'READY' if iteration else 'PROVISIONING',
                                  'leaseToken': secrets.token_hex(32), 'resourceKey': 'BBS:1', 'snapshotHash': 'a' * 64,
                                  'policyVersion': 'works-v1', 'mode': 'shadow',
                                  'budget': {'maxSteps': 20, 'maxSeconds': 90, 'maxTokens': 100000, 'maxSearches': 0}}, claimed=False, step=0)
                completed.clear()
                with open(Path(data) / f'cli-{iteration}.log', 'wb') as log:
                    process = await asyncio.create_subprocess_exec('sh', str(root / 'scripts/run-moderation-profile.sh'), cwd=root, env=env, stdout=log, stderr=log)
                    try:
                        await asyncio.wait_for(completed.wait(), 120)
                        assert process.returncode is None
                        log.flush()
                        assert not errors, {'reasons': [error.get('reason', error.get('exposed_tools')) for error in errors],
                                            'model_requests': len(requests), 'log': (Path(data) / f'cli-{iteration}.log').read_text(errors='replace')[-8000:]}
                        assert phase['step'] == 3
                    finally:
                        if process.returncode is None:
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 15)
                        except asyncio.TimeoutError:
                            process.kill()
                            await process.wait()
                        process = None
            assert len(requests) == 6
            assert len([item for item in calls if item[0] == 'submit']) == 2
            assert len([item for item in calls if item[0] == 'account']) >= 6
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            await runner.cleanup()
