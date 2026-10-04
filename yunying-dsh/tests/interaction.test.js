import test from 'node:test';
import assert from 'node:assert/strict';
import { SocialRuntime } from '../src/runtime.js';
import { FixtureStore,harness,ScriptAdapter,testSettings,textResponse,toolResponse,event,settle,durableEvents } from './helpers.js';

test('native Skill loads only the bundled Memory instructions and unread exposes usable source IDs',async()=>{
 const {ctx}=await harness();const runtime=new SocialRuntime(ctx,new FixtureStore(),testSettings(),async()=>({ok:true}));
 try{
  await runtime.accept(event('source-visible'));await settle(runtime);const conv=runtime.conversations.get('group:1001');
  const execute=(name,args)=>conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'fixture-'+name,name,arguments:args,signal:new AbortController().signal});
  const skill=await execute('skill',{name:'yunying-memory'});assert.equal(skill.isError,false);assert.ok(skill.content[0].text.includes('MySQL'));
  const unread=await execute('qq_get_unread_messages',{key:conv.state.key,token:conv.state.agentToken});
  assert.equal(JSON.parse(unread.content[0].text).messages[0].eventId,'source-visible');
  const forbidden=await execute('skill',{name:'arbitrary-filesystem'});assert.equal(forbidden.isError,true);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});

test('native Agent waits for arrivals, reads new messages, searches, replies once and wakes from finite diving',async()=>{
 const adapter=new ScriptAdapter(),{ctx}=await harness(adapter),sent=[];let searches=0;
 const config={...testSettings(),minQuietMs:10,waitMinMs:1,preSleepWaitMs:20,maxWakeMinute:100,maxWakeHour:100};
 const runtime=new SocialRuntime(ctx,new FixtureStore(),config,async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9002'};});
 try{
  await runtime.load('group:1001');const conv=runtime.conversations.get('group:1001');
  conv.webSearch=async query=>{searches++;assert.equal(query,'天气');return {results:[{title:'天气',url:'https://example.com/weather',snippet:'今天天气晴朗'}]};};
  const args=()=>({key:conv.state.key,token:conv.state.agentToken});
  adapter.script.push(()=>toolResponse('observe','qq_wait_for_messages',{...args(),timeoutMs:1000}),
   ()=>toolResponse('search','web_search',{...args(),query:'天气'}),
   ()=>toolResponse('answer','qq_send_message',{...args(),messages:'查到了，今天晴，出去走走正好。'}),
   ()=>toolResponse('close','qq_mark_read',{...args(),throughSeq:conv.state.readThrough()}));
  await runtime.accept(event('wait-first','group:1001',{atBot:true,text:'今天适合出门吗？'}));
  const arrival=setTimeout(()=>void runtime.accept(event('wait-new','group:1001',{text:'你看一下天气就好，晚安'})),15);
  await settle(runtime);clearTimeout(arrival);
  assert.equal(searches,1);assert.equal(sent.length,1);assert.equal(adapter.maxActive,1);
  const logs=await durableEvents(ctx,conv.state.sessionId);const result=logs.find(e=>e.type==='tool/result'&&e.data.message.toolCallId==='observe');
  const payload=JSON.parse(result.data.message.content[0].text);assert.equal(payload.arrived,true);assert.equal(payload.newMessages[0].eventId,'wait-new');
  assert.equal(conv.state.unread.length,0);
  // Shorten only the test timer; production tool contracts keep the donor's minute-scale sleep floor.
  adapter.script.push(textResponse(''));conv.state.wakeConfig.infinite=false;conv.state.wakeConfig.sleepUntil=new Date(Date.now()+15).toISOString();runtime.schedule(conv);
  await new Promise(resolve=>setTimeout(resolve,50));await settle(runtime);assert.equal(conv.state.lastWakeReason,'timeout');
 }finally{await runtime.close();await ctx.fiber.dispose();}
});


test('native image tool durably admits media, forwards image content and restores its reference',async()=>{
 const first=await harness(),store=new FixtureStore();let runtime=new SocialRuntime(first.ctx,store,testSettings(),async(path)=>{
  assert.equal(path,'/images');return {ok:true,isRecalled:false,images:[{mediaType:'image/webp',data:'UklGRjoAAABXRUJQVlA4IC4AAACQAQCdASoCAAIAAUAmJaACdLoAA5gA/vtV4/+lwf/S4P/pcH/pcH8bss4bpAAA'}]};
 });
 let ref,sessionId;
 try{
  await runtime.load('group:1001');const conv=runtime.conversations.get('group:1001');sessionId=conv.state.sessionId;
  first.adapter.script.push(()=>toolResponse('look-image','qq_get_message_images',{key:conv.state.key,token:conv.state.agentToken,messageId:'501'}),textResponse(''));
  await runtime.accept(event('image-input','group:1001',{messageId:'501',atBot:true,text:'看看这张图'}));await settle(runtime);
  const logs=await durableEvents(first.ctx,sessionId);
  const result=logs.find(e=>e.type==='tool/result'&&e.data.message.toolCallId==='look-image');
  assert.notEqual(result.data.message.isError,true);ref=result.data.message.content.find(c=>c.type==='image').attachment;
  assert.equal(ref.width,2);assert.equal(ref.height,2);assert.ok(ref.attachmentId);
  assert.ok(first.adapter.requests.some(request=>request.messages.some(message=>message.content.some(c=>c.type==='image'))));
  assert.ok(!JSON.stringify(result).includes('UklGRjoAAABXRUJQVlA4IC4AAACQAQCdASoCAAIAAUAmJaACdLoAA5gA/vtV4/+lwf/S4P/pcH/pcH8bss4bpAAA'));
 }finally{await runtime.close();await first.ctx.fiber.dispose();}
 const second=await harness(new ScriptAdapter(),first.root);runtime=new SocialRuntime(second.ctx,store,testSettings(),async()=>({ok:true}));
 try{
  await runtime.restore();assert.equal(runtime.conversations.get('group:1001').state.sessionId,sessionId);
  const restored=await second.ctx.attachments.readImage(ref);assert.equal(restored.ref.attachmentId,ref.attachmentId);assert.ok(restored.data.length>10);
 }finally{await runtime.close();await second.ctx.fiber.dispose();}
});

test('host failures do not expose SQL, paths or infrastructure diagnostics to the model',async()=>{
 const {ctx}=await harness(),store=new FixtureStore();store.memorySearch=async()=>{throw new Error('SELECT password FROM private /private/credentials');};
 store.audit=async()=>{};const runtime=new SocialRuntime(ctx,store,testSettings(),async()=>({ok:true}));
 try{
  await runtime.accept(event('diagnostic-boundary'));await settle(runtime);const conv=runtime.conversations.get('group:1001');
  const result=await conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'safe-error',name:'memory_search',
   arguments:{key:conv.state.key,token:conv.state.agentToken,query:''},signal:new AbortController().signal});
  assert.equal(result.isError,true);assert.ok(!/SELECT|password|private|credentials/.test(JSON.stringify(result)));
 }finally{await runtime.close();await ctx.fiber.dispose();}
});
