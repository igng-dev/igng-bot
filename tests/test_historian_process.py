"""Independent Python API + official Historian CLI, real SQL and synthetic model wire."""
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

pytestmark=pytest.mark.skipif(os.getenv('YUNYING_TEST_DB')!='yunying_v4_test' or os.getenv('YUNYING_RUN_PROFILE_SMOKE')!='1',reason='disposable database + process opt-in')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def test_independent_published_historian_profile_and_api():
    asyncio.run(scenario())


async def scenario():
    root=Path(__file__).resolve().parents[1]
    data=Path(tempfile.mkdtemp(prefix='historian-process-',dir='/tmp/opencode'))
    api_port,model_port=port(),port()
    admin,worker=secrets.token_hex(32),secrets.token_hex(32)
    server=1990000000+uuid4().int%1000000
    start_day=(datetime.now(timezone.utc)-timedelta(days=3)).date()
    label=start_day.isoformat()
    conn=pymysql.connect(host='127.0.0.1',port=int(os.getenv('YUNYING_TEST_DB_PORT','33316')),user='root',database='yunying_v4_test',autocommit=True)
    with conn.cursor() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS chat_messages (id BIGINT AUTO_INCREMENT PRIMARY KEY,server_id INT,sent_at DATETIME(3),player_uuid VARCHAR(36),player_name VARCHAR(64),content TEXT,source VARCHAR(16),cancelled BOOLEAN)')
        cur.execute('CREATE TABLE IF NOT EXISTS player_event_logs (id BIGINT AUTO_INCREMENT PRIMARY KEY,server_id INT,occurred_at DATETIME(3),player_uuid VARCHAR(36),player_name VARCHAR(64),event_type VARCHAR(32),reason TEXT)')
        cur.execute('INSERT INTO chat_messages(server_id,sent_at,player_uuid,player_name,content,source,cancelled) VALUES(%s,%s,%s,%s,%s,%s,0)',
                    (server,datetime.combine(start_day,datetime.min.time()),str(uuid4()),'Synthetic','合成玩家说你好。','paper'))
        event_id=f'mc:{server}:chat_messages:{cur.lastrowid}'
    conn.close()
    observed=[]
    async def model(request):
        body=await request.json();observed.append(body)
        names=[t['name'] for t in body.get('tools',[])]
        assert names and all(name=='skill' or name.startswith('historian_') for name in names),names
        assert 'online_seconds' not in json.dumps(body),'mechanical statistics leaked to model'
        step=len(observed)
        name,args=('historian_timeline',{'after':0}) if step==1 else ('historian_submit',{'observations':[{
            'title':'问候','summary':'合成玩家发出问候。','certainty':'fact','evidence_event_ids':[event_id]}]})
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await response.prepare(request)
        async def emit(kind,value):await response.write(('event: '+kind+'\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode())
        await emit('message_start',{'type':'message_start','message':{'id':str(uuid4()),'type':'message','role':'assistant','model':'fixture','content':[],
                   'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}})
        await emit('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':str(uuid4()),'name':name,'input':{}}})
        await emit('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(args,ensure_ascii=False)}})
        await emit('content_block_stop',{'type':'content_block_stop','index':0})
        await emit('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},'usage':{'output_tokens':5}})
        await emit('message_stop',{'type':'message_stop'});await response.write_eof();return response
    app=web.Application();app.router.add_post('/anthropic/v1/messages',model)
    runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'127.0.0.1',model_port).start()
    env={**os.environ,'DB_HOST':'127.0.0.1','DB_PORT':os.getenv('YUNYING_TEST_DB_PORT','33316'),'DB_USER':'root','DB_PASSWORD':'','DB_NAME':'yunying_v4_test',
         'MC_DB_HOST':'127.0.0.1','MC_DB_PORT':os.getenv('YUNYING_TEST_DB_PORT','33316'),'MC_DB_USER':'root','MC_DB_PASSWORD':'','MC_DB_NAME':'yunying_v4_test',
         'MC_DATABASE_MODE':'unified','SITE_AI_RECORDS_ENABLED':'0','HISTORIAN_SCHEDULE':'0','HISTORIAN_SERVERS':json.dumps({str(server):'UTC'}),
         'HISTORIAN_ADMIN_SECRET':admin,'HISTORIAN_DSH_SECRET':worker,'HISTORIAN_PORT':str(api_port),'HISTORIAN_API_URL':f'http://127.0.0.1:{api_port}',
         'HISTORIAN_PROVIDER':'deepseek-official','HISTORIAN_MODEL':'deepseek-v4-flash','HISTORIAN_PACKAGE_DIR':str(root/'historian-dsh'),
         'DSH_BIN':str(root/'yunying-dsh/node_modules/.bin/dsh'),'DSH_HOME':str(data/'dsh'),'DSH_TELEMETRY_DISABLED':'1',
         'DEEPSEEK_API_KEY':secrets.token_hex(32),'DEEPSEEK_BASE_URL':f'http://127.0.0.1:{model_port}/anthropic','NO_PROXY':'127.0.0.1,localhost'}
    processes=[];logs=[]
    async def spawn(name,command):
        log=open(data/(name+'.log'),'wb');logs.append(log)
        proc=await asyncio.create_subprocess_exec(*command,cwd=root,env=env,stdout=log,stderr=log);processes.append(proc)
    async def wait_until(check,seconds=120):
        end=asyncio.get_running_loop().time()+seconds
        while asyncio.get_running_loop().time()<end:
            if any(p.returncode is not None for p in processes):raise AssertionError(f'process exited; fixture logs: {data}')
            try:
                if await check():return
            except (aiohttp.ClientError,OSError):pass
            await asyncio.sleep(.2)
        raise AssertionError(f'fixture timed out; logs: {data}')
    try:
        await spawn('api',[sys.executable,'-m','igngbot_v4.historian'])
        async with aiohttp.ClientSession() as client:
            async def healthy():
                async with client.get(f'http://127.0.0.1:{api_port}/health') as response:return response.status==200
            await wait_until(healthy)
            async with client.post(f'http://127.0.0.1:{api_port}/admin/create',headers={'Authorization':'Bearer '+admin},json={
                    'server_id':server,'kind':'daily','period_start':label,'actor':'site:fixture','request_key':str(uuid4())}) as response:
                assert response.status==200,await response.text();created=await response.json()
            await spawn('dsh',['sh',str(root/'scripts/run-historian-profile.sh')])
            async def completed():
                db=pymysql.connect(host='127.0.0.1',port=int(env['DB_PORT']),user='root',database='yunying_v4_test')
                try:
                    with db.cursor() as cur:
                        cur.execute('SELECT status,error FROM historian_runs WHERE id=%s',(created['run_id'],));status,error=cur.fetchone()
                        if status=='failed':raise AssertionError(f'run failed: {error}; logs: {data}')
                        return status=='completed'
                finally:db.close()
            await wait_until(completed,180)
        assert len(observed)==2
        db=pymysql.connect(host='127.0.0.1',port=int(env['DB_PORT']),user='root',database='yunying_v4_test')
        try:
            with db.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM historian_calls WHERE run_id=%s',(created['run_id'],));assert cur.fetchone()[0]>=3
                cur.execute('SELECT current_run_id FROM historian_reports WHERE id=%s',(created['report_id'],));assert cur.fetchone()[0]==created['run_id']
        finally:db.close()
    finally:
        for process in processes:
            if process.returncode is None:process.terminate()
        for process in processes:
            try:await asyncio.wait_for(process.wait(),15)
            except asyncio.TimeoutError:process.kill();await process.wait()
        for log in logs:log.close()
        await runner.cleanup()
