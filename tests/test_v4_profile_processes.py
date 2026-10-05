"""Real DSH CLI + Python processes with wire-level OneBot/Messages fixtures; no real QQ or paid model."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import tempfile
from uuid import uuid4

import aiohttp
from aiohttp import web
import pymysql
import pytest

pytestmark = pytest.mark.skipif(os.getenv('YUNYING_RUN_PROFILE_SMOKE') != '1' or os.getenv('YUNYING_TEST_DB') != 'yunying_v4_test', reason='explicit disposable DB + process smoke opt-in')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


async def eventually(check, timeout=40):
    end = asyncio.get_running_loop().time()+timeout
    while asyncio.get_running_loop().time()<end:
        try:
            if await check():
                return
        except (OSError,aiohttp.ClientError):
            pass
        await asyncio.sleep(.05)
    raise AssertionError('process smoke timed out')


def test_actual_profile_process_restart_and_qq_wire_tools():
    asyncio.run(process_scenario())


async def process_scenario():
    root = Path(__file__).resolve().parents[1]
    cli = os.getenv('YUNYING_TEST_DSH_BIN',str(root/'yunying-dsh/node_modules/.bin/dsh'))
    data = Path(tempfile.mkdtemp(prefix='yunying-v4-process-'))
    (data/'media').mkdir()
    server_port,dsh_port,infra_port=free_port(),free_port(),free_port()
    group=str(2000000000+int(uuid4().int%10**8))
    key='group:'+group
    secret=secrets.token_hex(32)
    admin_secret=secrets.token_hex(32)
    received=[]
    websocket_ready=asyncio.Event()
    sockets=[]
    counters={'requests':0,'active':0,'max_active':0,'searches':0}
    phase={'action':'silence','step':0,'doc':None}
    gate=asyncio.Event()
    gate.set()

    async def ws(request):
        peer=web.WebSocketResponse()
        await peer.prepare(request)
        sockets.append(peer)
        websocket_ready.set()
        async for _ in peer:
            pass
        return peer

    async def send(request):
        received.append(await request.json())
        return web.json_response({'status':'ok','retcode':0,'data':{'message_id':100000+len(received)}})

    async def messages(request):
        body=await request.json()
        if any(t.get('type','').startswith('web_search') for t in body.get('tools',[])):
            counters['searches']+=1
            return web.json_response({'id':'search-fixture','type':'message','role':'assistant','model':'script','stop_reason':'end_turn','stop_sequence':None,
              'usage':{'input_tokens':10,'output_tokens':5},'content':[{'type':'web_search_tool_result','tool_use_id':'search-fixture',
               'content':[{'type':'web_search_result','title':'天气预报','url':'https://example.org/weather','encrypted_content':'fixture','page_age':'today'}]}]})
        names=[tool['name'] for tool in body.get('tools',[])]
        assert names and all(name=='skill' or name.startswith(('qq_','memory_','web_','mcp__snowluma__','mcp__web-search-safe__')) for name in names), 'published Profile exposed a non-social host tool'
        counters['requests']+=1
        counters['active']+=1
        counters['max_active']=max(counters['max_active'],counters['active'])
        try:
            await gate.wait()
            text=json.dumps(body,ensure_ascii=False)
            tokens=re.findall(r'【会话令牌】([0-9a-f]{32})',text)
            common={'key':key,'token':tokens[-1]} if tokens else {}
            tool=None
            args=None
            if phase['action']=='remember':
                step=phase['step']
                if step==0:
                    # Select the latest real user event exposed by the official Messages request.
                    ids=re.findall(r'\\?"eventId\\?":\\?"([0-9a-f-]{36})',text)
                    assert ids, 'native event source was not presented to the model'
                    tool='memory_write'
                    args={**common,'title':'茶偏好','markdown':'# 偏好\n喜欢红茶','sources':[ids[-1]],'reason':'本人要求记忆','personQQ':'2001'}
                elif step==1:
                    tool='web_search';args={**common,'query':'天气'}
                elif step==2:
                    tool='qq_send_message';args={**common,'messages':'记住了，喜欢红茶。查到今天晴，出门走走也不错。'}
                phase['step']+=1
            elif phase['action']=='recall':
                if phase['step']==0:
                    tool='memory_read';args={**common,'id':phase['doc']}
                elif phase['step']==1:
                    tool='qq_reply';args={**common,'message':'记得，你喜欢红茶。','replyToMessageId':phase['incoming_id']}
                phase['step']+=1
            content={'type':'tool_use','id':str(uuid4()),'name':tool,'input':{}} if tool else {'type':'text','text':''}
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            async def emit(name,value):
                await response.write(('event: '+name+'\ndata: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode())
            await emit('message_start',{'type':'message_start','message':{'id':str(uuid4()),'type':'message','role':'assistant','model':'deepseek-v4-flash','content':[],
                'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}})
            await emit('content_block_start',{'type':'content_block_start','index':0,'content_block':content})
            if tool:
                await emit('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(args,ensure_ascii=False)}})
            await emit('content_block_stop',{'type':'content_block_stop','index':0})
            await emit('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use' if tool else 'end_turn','stop_sequence':None},'usage':{'output_tokens':5}})
            await emit('message_stop',{'type':'message_stop'})
            await response.write_eof()
            return response
        finally:
            counters['active']-=1

    app=web.Application()
    app.router.add_get('/ws',ws)
    app.router.add_post('/onebot/send_group_msg',send)
    app.router.add_post('/anthropic/v1/messages',messages)
    runner=web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner,'127.0.0.1',server_port).start()
    env={**os.environ,'DSH_HOME':str(data/'dsh'),'DSH_TELEMETRY_DISABLED':'1','YUNYING_PACKAGE_DIR':str(root/'yunying-dsh'),'DSH_BIN':cli,
      'YUNYING_INTERNAL_SECRET':secret,'YUNYING_ADMIN_SECRET':admin_secret,'YUNYING_ALLOW_GROUPS':group,'YUNYING_ALLOW_PRIVATE':'',
      'YUNYING_DSH_PORT':str(dsh_port),'YUNYING_INFRA_PORT':str(infra_port),'YUNYING_DSH_URL':f'http://127.0.0.1:{dsh_port}','YUNYING_INFRA_URL':f'http://127.0.0.1:{infra_port}',
      'DB_HOST':'127.0.0.1','DB_PORT':os.getenv('YUNYING_TEST_DB_PORT','33316'),'DB_USER':'root','DB_PASSWORD':'','DB_NAME':'yunying_v4_test',
      'ONEBOT_WS_URL':f'ws://127.0.0.1:{server_port}/ws','ONEBOT_HTTP_URL':f'http://127.0.0.1:{server_port}/onebot','BOT_USER_ID':'3001',
      'ONEBOT_ACCESS_TOKEN':'','ONEBOT_HTTP_TOKEN':'','MESSAGE_ROOT':str(data/'media'),'LOCAL_STORAGE':str(data/'infra'),
      'STORAGE_REQUIRE_MOUNT':'0','MC_TICKET_NOTIFICATION_GROUP':'0','MC_TICKET_TECH_NOTIFICATION_GROUP':'0',
      'SITE_AI_RECORDS_ENABLED':'0','DEEPSEEK_API_KEY':secrets.token_hex(32),'DEEPSEEK_BASE_URL':f'http://127.0.0.1:{server_port}/anthropic',
      'YUNYING_SEARCH_PROVIDER':'deepseek','DEEPSEEK_SEARCH_BASE_URL':f'http://127.0.0.1:{server_port}/anthropic/v1','NO_PROXY':'127.0.0.1,localhost'}
    def set_pause(enabled):
        conn = pymysql.connect(host='127.0.0.1', port=int(env['DB_PORT']), user='root', database='yunying_v4_test', autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO group_configs (group_id,is_chat_mode,social_paused) VALUES (%s,1,%s) ON DUPLICATE KEY UPDATE social_paused=VALUES(social_paused)', (group, int(not enabled)))
        finally:
            conn.close()
    def set_chat_mode(enabled):
        conn = pymysql.connect(host='127.0.0.1',port=int(env['DB_PORT']),user='root',database='yunying_v4_test',autocommit=True)
        try:
            with conn.cursor() as cur:cur.execute('UPDATE group_configs SET is_chat_mode=%s WHERE group_id=%s',(int(enabled),group))
        finally:conn.close()
    set_pause(True)
    processes=[]
    logs=[]
    async def start_processes():
        for name,cmd in [('dsh',['sh',str(root/'scripts/run-yunying-profile.sh')]),('infra',[sys.executable,'-m','igngbot_v4'])]:
            log=open(data/(name+'-'+str(len(logs))+'.log'),'wb')
            logs.append(log)
            processes.append(await asyncio.create_subprocess_exec(*cmd,cwd=root,env=env,stdout=log,stderr=log))
        async with aiohttp.ClientSession() as client:
            async def healthy():
                if any(p.returncode is not None for p in processes):
                    raise RuntimeError('fixture process exited before readiness; inspect test logs')
                for port in [dsh_port,infra_port]:
                    async with client.get(f'http://127.0.0.1:{port}/health') as resp:
                        if resp.status!=200 or not (await resp.json())['ok']:
                            return False
                return True
            await eventually(healthy,90)
        await asyncio.wait_for(websocket_ready.wait(),15)
    async def stop_processes():
        gate.set()
        for process in processes:
            if process.returncode is None:
                process.terminate()
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(),15)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        processes.clear()
        for log in logs:
            log.flush()
    async def incoming(mid,text,at=False,reply=None,user='2001'):
        segments=([{'type':'at','data':{'qq':'3001'}}] if at else [])+([{'type':'reply','data':{'id':str(reply)}}] if reply else [])+[{'type':'text','data':{'text':text}}]
        await sockets[-1].send_json({'post_type':'message','message_type':'group','group_id':int(group),'user_id':int(user),'self_id':3001,
          'message_id':mid,'time':1700000000+mid,'message':segments,'sender':{'role':'member','nickname':'群友'}})
    try:
        await start_processes()
        await incoming(1,'普通闲聊，风很舒服')
        await eventually(lambda: asyncio.sleep(0,result=counters['requests']>=1))
        await asyncio.sleep(.3)
        assert not received
        set_chat_mode(False)
        phase.update(action='remember',step=0)
        gate.clear()
        await incoming(2,'记住我喜欢红茶，顺便查下天气',at=True)
        await eventually(lambda:asyncio.sleep(0,result=counters['active']>0))
        for i in range(3,13):
            await incoming(i,'大家聊聊今天的风',user=str(2000+i))
        async with aiohttp.ClientSession() as client:
            async def all_queued():
                conn=pymysql.connect(host='127.0.0.1',port=int(env['DB_PORT']),user='root',database='yunying_v4_test')
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM yunying_events WHERE conversation_key=%s AND JSON_EXTRACT(payload,'$.isConfiguration') IS NULL",(key,))
                        return cur.fetchone()[0]==12
                finally:
                    conn.close()
            await eventually(all_queued)
            gate.set()
            await eventually(lambda:asyncio.sleep(0,result=len(received)==1))
            assert counters['searches']==1 and counters['max_active']==1
            async def memory_ready():
                async with client.post(f'http://127.0.0.1:{dsh_port}/admin/memory/search',json={'query':'红茶'},headers={'Authorization':'Bearer '+admin_secret}) as response:
                    docs=(await response.json())['documents']
                    found=[d for d in docs if d.get('scope_key')==key]
                    if found:
                        phase['doc']=found[0]['id']
                        return True
                    return False
            await eventually(memory_ready)
        def mapping():
            conn=pymysql.connect(host='127.0.0.1',port=int(env['DB_PORT']),user='root',database='yunying_v4_test')
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT dsh_session_id FROM yunying_sessions WHERE conversation_key=%s',(key,))
                    return cur.fetchone()[0]
            finally:
                conn.close()
        await asyncio.sleep(.3)
        before=mapping()
        # Kill only the two fixture processes' own DB lease connections. Both must exit,
        # allowing a real supervisor to restart them; a health failure alone is insufficient.
        conn=pymysql.connect(host='127.0.0.1',port=int(env['DB_PORT']),user='root',database='yunying_v4_test',autocommit=True)
        try:
            with conn.cursor() as cur:
                for lock_prefix in ['yunying-dsh:','yunying-onebot:']:
                    cur.execute('SELECT IS_USED_LOCK(%s)',(lock_prefix+'yunying_v4_test',))
                    own_connection=cur.fetchone()[0]
                    assert own_connection is not None
                    cur.execute('KILL '+str(int(own_connection)))
        finally:
            conn.close()
        await eventually(lambda:asyncio.sleep(0,result=all(p.returncode is not None for p in processes)),20)
        await stop_processes()
        websocket_ready.clear()
        phase.update(action='recall',step=0,incoming_id='13')
        await start_processes()
        assert mapping()==before
        await incoming(13,'你还记得我喜欢什么吗',at=True,reply=100001)
        await eventually(lambda:asyncio.sleep(0,result=len(received)==2))
        assert received[-1]['message'][0]=={'type':'reply','data':{'id':'13'}}
        assert counters['max_active']==1
        # The hard pause was removed. The legacy social_paused column is inert: even
        # with it set, an explicit call still wakes the model and messages keep flowing.
        phase.update(action='silence',step=0)
        requests=counters['requests']
        set_pause(False)
        await incoming(14,'暂停已移除，呼叫仍应处理',at=True)
        await eventually(lambda:asyncio.sleep(0,result=counters['requests']>requests))
        assert len(received)==2 and counters['max_active']==1
        set_pause(True)
        await incoming(15,'继续闲聊',at=True)
        await eventually(lambda:asyncio.sleep(0,result=counters['requests']>requests+1))
        assert mapping()==before and counters['max_active']==1
    finally:
        await stop_processes()
        for log in logs:
            log.close()
        await runner.cleanup()
