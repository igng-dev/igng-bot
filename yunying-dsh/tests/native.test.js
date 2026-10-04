import test from 'node:test';
import assert from 'node:assert/strict';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { renderPrompt } from '@deepseek-ai/dsh-system-prompt';
import { SocialRuntime, RESERVED2_PROMPT } from '../src/runtime.js';
import { FixtureStore,harness,ScriptAdapter,testSettings,textResponse,toolResponse,event,settle,durableEvents } from './helpers.js';

test('native DSH: ordinary silence, tool-only sending, exact scoped permission boundary',async()=>{
 const {ctx,adapter}=await harness(new ScriptAdapter([textResponse('不会自动发送的普通文本')]));
 const store=new FixtureStore(),sent=[];const runtime=new SocialRuntime(ctx,store,testSettings(),async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9001'};});
 try {
  await runtime.accept(event('1'));await settle(runtime);assert.equal(sent.length,0);assert.equal(adapter.requests.length,1);
  const conv=runtime.conversations.get('group:1001'),agent=conv.handle.agent;
  assert.ok(RESERVED2_PROMPT.includes('【等、回与沉睡前观察】'));
  const actualSystem=adapter.requests[0].messages.filter(m=>m.role==='system').flatMap(m=>m.content).filter(c=>c.type==='text').map(c=>c.text).join('\n');
  const expectedPersona=renderPrompt({sections:[{name:'donor',text:RESERVED2_PROMPT}],contexts:[],tools:[],variables:{model:adapter.requests[0].model}});
  assert.ok(actualSystem.includes(expectedPersona),'original donor template reaches the real request with only upstream interpolation');
  ctx.tools.register({name:'danger',description:'untrusted extra host tool',parameters:{type:'object',properties:{},additionalProperties:false},output:{schema:{type:'object',additionalProperties:true},render:()=>[]},execute:async()=>({ok:true})});
  assert.ok(!agent.ctx.tools.schemas(agent).some(t=>t.name==='danger'));
  const args={key:conv.state.key,token:conv.state.agentToken,messages:'我在呢'};
  adapter.script.push(()=>toolResponse('send-direct','qq_send_message',args),textResponse(''));
  await runtime.accept(event('2','group:1001',{atBot:true,text:'@云萤 在吗',plain:'@云萤 在吗'}));await settle(runtime);
  assert.equal(sent.length,1);assert.equal(sent[0].path,'/send');assert.equal(sent[0].body.message,'我在呢');assert.equal(adapter.maxActive,1);
  const denied=await agent.ctx.tools.execute({agent,callId:'deny-cross',name:'qq_get_unread_messages',arguments:{key:'group:1002',token:args.token},signal:new AbortController().signal});
  assert.equal(denied.isError,true);
  const hidden=await agent.ctx.tools.execute({agent,callId:'deny-shell',name:'danger',arguments:{},signal:new AbortController().signal});assert.equal(hidden.isError,true);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});

test('native DSH: every concurrent event persists once and same-session model work is serial',async()=>{
 let unblock;const gate=new Promise(resolve=>unblock=resolve);
 const adapter=new ScriptAdapter([async()=>{await gate;return textResponse('');}]);
 const {ctx}=await harness(adapter);const store=new FixtureStore(),runtime=new SocialRuntime(ctx,store,testSettings(),async()=>({ok:true}));
 try {
  await runtime.accept(event('10','group:1001',{atBot:true}));
  await Promise.all(Array.from({length:20},(_,i)=>runtime.accept(event(String(i+11),'group:1001',{userId:String(2100+i),atBot:true}))));
  unblock();await settle(runtime);const conv=runtime.conversations.get('group:1001');
  const logs=await durableEvents(ctx,conv.state.sessionId);
  const staged=logs.filter(e=>e.type==='agent/inbox/spliced').flatMap(e=>e.data.inserted).filter(m=>m.content.some(c=>c.text?.startsWith('【QQ事件')));
  assert.equal(staged.length,21);assert.equal(new Set(staged.map(m=>m.id)).size,21);assert.equal(adapter.maxActive,1);
  assert.equal(store.maps.size,1);assert.equal([...store.rows.values()].filter(r=>r.delivered).length,21);
  await runtime.accept(event('10','group:1001',{atBot:true}));assert.equal(store.rows.size,21);
 }finally{unblock();await runtime.close();await ctx.fiber.dispose();}
});

test('official JSONL recovery: SQL acknowledgement failure and native dispose retain one pending logical input',async()=>{
 const store=new FixtureStore();let first=await harness(),runtime=new SocialRuntime(first.ctx,store,testSettings(),async()=>({ok:true}));
 // A paused conversation lets us inspect the official durable inbox without consuming it.
 await runtime.load('group:1001');const before=runtime.conversations.get('group:1001');before.state.paused=true;await store.setPaused('group:1001',true);
 store.failDeliveryOnce=true;await assert.rejects(runtime.accept(event('recover')));
 const sessionId=before.state.sessionId;await runtime.close();await first.ctx.fiber.dispose();
 const second=await harness(new ScriptAdapter(),first.root);runtime=new SocialRuntime(second.ctx,store,testSettings(),async()=>({ok:true}));
 try{
  await runtime.restore();const after=runtime.conversations.get('group:1001');assert.equal(after.state.sessionId,sessionId);
  assert.equal(after.handle.agent.inbox.nextStep.filter(m=>m.content.some(c=>c.text?.includes('recover'))).length,1);
  assert.equal(store.rows.get('recover').delivered,1);
  const logs=await durableEvents(second.ctx,sessionId);assert.equal(logs.filter(e=>e.type==='agent/inbox/spliced').flatMap(e=>e.data.inserted).filter(m=>m.content.some(c=>c.text?.startsWith('【QQ事件'))).length,2);
  // Native disposal durably cancels the first insertion. Recovery re-admits the same input id exactly once.
  assert.equal(after.handle.agent.inbox.nextStep.length,1);
 }finally{await runtime.close();await second.ctx.fiber.dispose();}
});

test('real official DSH Compaction continues after Session restart; summary stays separate from Memory',async()=>{
 const adapter=new ScriptAdapter(),first=await harness(adapter),store=new FixtureStore();
 let runtime=new SocialRuntime(first.ctx,store,testSettings(),async()=>({ok:true})),sessionId;
 try{
  for(let n=0;n<4;n++){adapter.script.push(textResponse('答复'.repeat(200)));await runtime.accept(event(`compact-${n}`,'group:1001',{atBot:true,text:'历史问题'.repeat(200)}));await settle(runtime);}
  const conv=runtime.conversations.get('group:1001');sessionId=conv.state.sessionId;adapter.script.push(textResponse('保留 QQ 群聊上下文、身份及工具权限的压缩摘要。'));
  const result=await first.ctx.compaction.compactNow(conv.handle.agent,new AbortController().signal);assert.ok(result);
  adapter.script.push(textResponse(''));await runtime.accept(event('post-compact','group:1001',{replyToBot:true}));await settle(runtime);
  const logs=await durableEvents(first.ctx,sessionId);assert.ok(logs.some(e=>e.type==='compaction/summary'));assert.ok(logs.some(e=>e.type==='user/message'&&e.data.content.some(c=>c.text?.includes('post-compact'))));
  assert.equal(store.maps.size,1);assert.equal(typeof store.memoryWrite,'undefined');
 }finally{await runtime.close();await first.ctx.fiber.dispose();}
 const second=await harness(new ScriptAdapter(),first.root);runtime=new SocialRuntime(second.ctx,store,testSettings(),async()=>({ok:true}));
 try{
  await runtime.restore();assert.equal(runtime.conversations.get('group:1001').state.sessionId,sessionId);
  await runtime.accept(event('restart-after-compact','group:1001',{atBot:true}));await settle(runtime);
  const logs=await durableEvents(second.ctx,sessionId);assert.ok(logs.some(e=>e.type==='compaction/summary'));
  assert.ok(logs.some(e=>e.type==='user/message'&&e.data.content.some(c=>c.text?.includes('restart-after-compact'))));
 }finally{await runtime.close();await second.ctx.fiber.dispose();}
});


test('authorized resume drives queued native Inbox while pause prevents model activity',async()=>{
 const {ctx,adapter}=await harness(),store=new FixtureStore(),runtime=new SocialRuntime(ctx,store,testSettings(),async()=>({ok:true}));
 try{
  await runtime.accept(event('pause-command','group:1001',{commandHandled:true,pause:true}));
  await runtime.accept(event('queued-while-paused','group:1001',{atBot:true}));
  assert.equal(adapter.requests.length,0);
  await runtime.accept(event('resume-command','group:1001',{commandHandled:true,pause:false}));await settle(runtime);
  assert.equal(adapter.requests.length,1);assert.equal(runtime.conversations.get('group:1001').state.lastWakeReason,'resume');
  assert.ok(adapter.requests[0].messages.some(m=>m.content.some(c=>c.text?.includes('queued-while-paused'))));
 }finally{await runtime.close();await ctx.fiber.dispose();}
});
