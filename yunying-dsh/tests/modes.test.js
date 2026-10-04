import test from 'node:test';
import assert from 'node:assert/strict';
import { LlmError } from '@deepseek-ai/dsh-llm';
import { SocialRuntime } from '../src/runtime.js';
import { FixtureStore,harness,ScriptAdapter,testSettings,textResponse,toolResponse,event,settle,durableEvents } from './helpers.js';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

// This exercises the real official loop, Inbox, tools, persistence and lifecycle hooks.
test('call-only groups observe every event but only explicit @ or reply drives a turn, with bounded current context',async()=>{
 const store=new FixtureStore();await store.mapping('group:1001');store.maps.get('group:1001').chatMode=false;
 let h=await harness(),runtime=new SocialRuntime(h.ctx,store,testSettings(),async()=>({ok:true,message_id:'9001'}));
 try {
  for(let n=0;n<45;n++)await runtime.accept(event('off-'+n,'group:1001',{text:`普通闲聊 ${n}`}));
  const conv=runtime.conversations.get('group:1001'),id=conv.state.sessionId;
  assert.equal(h.adapter.requests.length,0);assert.equal(conv.handle.agent.inbox.hasPending,false);assert.equal(conv.timers.size,0);
  const args={key:conv.state.key,token:conv.state.agentToken,messages:'我在呢'};
  h.adapter.script.push(toolResponse('called-send','qq_send_message',args),textResponse('结束本轮'));
  await runtime.accept(event('explicit-call','group:1001',{atBot:true,text:'@云萤 现在聊到哪了'}));await settle(runtime);
  assert.equal(h.adapter.requests.length,2);
  const input=JSON.stringify(h.adapter.requests[0].messages);
  assert.ok(input.includes('普通闲聊 44')&&input.includes('explicit-call'));
  assert.ok(!input.includes('普通闲聊 0'));
  assert.equal(conv.directEventId,null);assert.equal(store.maps.get(conv.state.key).directEventId,null);
  await pause(180);assert.equal(h.adapter.requests.length,2,'no donor reminder can escape call-only mode');
  for(const reason of ['bootstrap','resume','timeout','replyCheck','proactiveCheck'])await runtime.wake(conv,reason,true);
  assert.equal(h.adapter.requests.length,2);
  const denied=await conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'out-of-turn-send',name:'qq_send_message',arguments:args,signal:new AbortController().signal});
  assert.equal(denied.isError,true);
  await runtime.close();await h.ctx.fiber.dispose();
  h=await harness(new ScriptAdapter(),h.root);runtime=new SocialRuntime(h.ctx,store,testSettings(),async()=>({ok:true}));await runtime.restore();
  assert.equal(runtime.conversations.get('group:1001').state.sessionId,id);assert.equal(h.adapter.requests.length,0);
  await runtime.accept(event('reply-call','group:1001',{replyToBot:true}));await settle(runtime);assert.equal(h.adapter.requests.length,1);
  await runtime.accept(event('off-after-reply'));assert.equal(h.adapter.requests.length,1);
  await runtime.close();await h.ctx.fiber.dispose();store.maps.get('group:1001').chatMode=true;
  h=await harness(new ScriptAdapter(),h.root);runtime=new SocialRuntime(h.ctx,store,testSettings(),async()=>({ok:true}));await runtime.restore();
  assert.equal(h.adapter.requests.length,0,'enabling before restart must not admit old observation-only events');
  assert.equal(runtime.conversations.get('group:1001').handle.agent.inbox.hasPending,false);
 }finally{await runtime.close();await h.ctx.fiber.dispose();}
});

test('current database mode gates native pre-step and timers, without changing Session identity or rewriting donor wake config',async()=>{
 const store=new FixtureStore(),h=await harness(),runtime=new SocialRuntime(h.ctx,store,testSettings(),async()=>({ok:true}));
 try {
  await runtime.accept(event('enabled','group:1001',{atBot:true}));await settle(runtime);
  const conv=runtime.conversations.get('group:1001'),id=conv.state.sessionId;
  const before=h.adapter.requests.length;
  store.maps.get('group:1001').chatMode=false;
  await runtime.refreshPolicies();
  assert.equal(conv.timers.size,0);
  await runtime.accept(event('question','group:1001',{text:'云萤，现在几点？'}));
  await runtime.wake(conv,'proactiveCheck');assert.equal(h.adapter.requests.length,before);
  // Even a queued native followup injected by a host cannot bypass the pre-step gate.
  conv.handle.agent.followup('unauthorized autonomous input');await settle(runtime);assert.equal(h.adapter.requests.length,before);
  store.maps.get('group:1001').chatMode=true;await runtime.refreshPolicies();
  assert.equal(h.adapter.requests.length,before,'turning on alone does not replay a backlog');
  await runtime.accept(event('new-on-message','group:1001',{atBot:true}));await settle(runtime);
  assert.equal(h.adapter.requests.length,before+1);assert.equal(conv.state.sessionId,id);
 }finally{await runtime.close();await h.ctx.fiber.dispose();}
});

test('call-only wait observes ordinary arrivals inside the called turn, and wake config cannot grant a later autonomous turn',async()=>{
 const store=new FixtureStore();await store.mapping('group:1001');store.maps.get('group:1001').chatMode=false;
 const h=await harness(),config={...testSettings(),minQuietMs:10,waitMinMs:10,preSleepWaitMs:10};
 const runtime=new SocialRuntime(h.ctx,store,config,async()=>({ok:true,message_id:'9002'}));
 try{
  const conv=await runtime.load('group:1001'),common={key:conv.state.key,token:conv.state.agentToken};
  h.adapter.script.push(toolResponse('wait-off','qq_wait_for_messages',{...common,timeoutMs:1000,quietMs:10}),
   toolResponse('set-active','qq_set_wake_config',{...common,config:{mode:'active',infinite:true}}));
  await runtime.accept(event('call-and-wait','group:1001',{atBot:true}));
  for(let n=0;n<50&&!conv.state.waiting;n++)await pause(5);
  assert.equal(conv.state.waiting,true);
  await runtime.accept(event('context-arrival','group:1001',{text:'补充一个细节'}));await settle(runtime);
  assert.equal(conv.state.wakeConfig.mode,'active');
  assert.ok(JSON.stringify(h.adapter.requests[1].messages).includes('context-arrival'));
  const before=h.adapter.requests.length;await runtime.accept(event('uninvited-new'));await pause(40);
  assert.equal(h.adapter.requests.length,before);assert.equal(conv.timers.size,0);
 }finally{await runtime.close();await h.ctx.fiber.dispose();}
});

test('native accounting groups tool continuations in one task and keeps compaction separate; replay retains event identities',async()=>{
 const store=new FixtureStore(),h=await harness(),runtime=new SocialRuntime(h.ctx,store,testSettings(),async()=>({ok:true,message_id:'9003'}));
 try {
  const conv=await runtime.load('group:1001'),common={key:conv.state.key,token:conv.state.agentToken};
  h.adapter.script.push(toolResponse('read-one','qq_get_unread_messages',common),
    toolResponse('send-one','qq_send_message',{...common,messages:'看到了'}),textResponse('结束'));
  await runtime.accept(event('account-call','group:1001',{atBot:true}));await settle(runtime);await Promise.all([...conv.recordTasks]);
  const records=store.calls.map(row=>row[3]),attempts=records.filter(r=>r.record_kind==='attempt');
  assert.equal(attempts.length,3);assert.equal(new Set(attempts.map(r=>r.task_key)).size,1);
  assert.deepEqual(attempts.map(r=>r.attempt_no),[1,2,3]);assert.ok(records.some(r=>r.record_kind==='task-end'&&r.task_status==='success'));
  h.adapter.script.push(textResponse('compaction summary'));
  await h.ctx.compaction.compactNow(conv.handle.agent,new AbortController().signal);await Promise.all([...conv.recordTasks]);
  const compact=store.calls.map(row=>row[3]).filter(r=>r.task_type==='dsh_compaction');
  assert.equal(compact.filter(r=>r.record_kind==='attempt').length,1);
  assert.ok(compact.every(r=>!attempts.some(a=>a.task_key===r.task_key)));
  const before=store.calls.map(row=>[row[0],row[3].task_key,row[3].attempt_no]);
  await runtime.close();await h.ctx.fiber.dispose();
  const restored=await harness(new ScriptAdapter(),h.root),next=new SocialRuntime(restored.ctx,store,testSettings(),async()=>({ok:true}));
  try{await next.restore();const after=store.calls.slice(before.length).map(row=>[row[0],row[3].task_key,row[3].attempt_no]);
    assert.deepEqual(after,before,'native history reconstructs identical accounting keys');
  }finally{await next.close();await restored.ctx.fiber.dispose();}
 }finally{await runtime.close();await h.ctx.fiber.dispose();}
});


test('official request retries remain attempts of one turn and missing provider usage remains unknown',async()=>{
 const fail=()=>{throw new LlmError('synthetic upstream unavailable','SERVER');};
 const h=await harness(new ScriptAdapter([fail,fail,textResponse('选择沉默')])),store=new FixtureStore();
 const runtime=new SocialRuntime(h.ctx,store,testSettings(),async()=>({ok:true}));
 h.ctx.on('agent/request-error',async()=>({kind:'retry'}));
 try {
  await runtime.accept(event('retry-accounting','group:1001',{atBot:true}));await settle(runtime);
  const conv=runtime.conversations.get('group:1001');await Promise.all([...conv.recordTasks]);
  const attempts=store.calls.map(row=>row[3]).filter(r=>r.record_kind==='attempt');
  assert.equal(attempts.length,3);assert.deepEqual(attempts.map(r=>r.attempt_no),[1,2,3]);
  assert.equal(new Set(attempts.map(r=>r.task_key)).size,1);
  assert.deepEqual(attempts.map(r=>r.token_usage===null),[true,true,false]);
  assert.deepEqual(attempts.map(r=>r.success),[false,false,true]);
  assert.equal(h.adapter.requests.length,3);assert.equal(h.adapter.maxActive,1);
 }finally{await runtime.close();await h.ctx.fiber.dispose();}
});
