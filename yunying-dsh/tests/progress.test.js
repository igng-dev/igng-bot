import test from 'node:test';
import assert from 'node:assert/strict';
import { SocialRuntime } from '../src/runtime.js';
import { progressMode, heartbeatText, forwardableText, emptyProgress } from '../src/progress.js';
import { FixtureStore,harness,ScriptAdapter,testSettings,textResponse,toolResponse,textToolResponse,event,settle } from './helpers.js';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('progress helpers: mode selection, bounded text and heartbeat wording',()=>{
 assert.equal(progressMode({progressReports:'auto',progressModelsWithout:new Set(['silent']),model:'silent'}),'heartbeat');
 assert.equal(progressMode({progressReports:'auto',progressModelsWithout:new Set(),model:'deepseek-v4-flash'}),'forward');
 assert.equal(progressMode({progressReports:'off',progressModelsWithout:new Set(['silent']),model:'silent'}),'off');
 // Reasoning is not process output and blank text is not worth a message.
 assert.equal(forwardableText([{type:'reasoning',text:'内部推理不算'},{type:'text',text:'  '}]),null);
 assert.equal(forwardableText([{type:'text',text:'查一下最近的记录'}]),'查一下最近的记录');
 const cut=forwardableText([{type:'text',text:'字'.repeat(500)}]);
 assert.equal(cut.length,400);assert.ok(cut.endsWith('…'));
 const progress=emptyProgress(3,1000);
 assert.equal(heartbeatText(progress,31000),'任务进行中（已 30 秒）：正在思考…');
 progress.commands=2;progress.searches=1;progress.steps=4;
 assert.equal(heartbeatText(progress,31000),'任务进行中（已 30 秒）：运行了 2 条命令、1 次搜索、4 次思考。');
});

test('forward mode sends interim text with tool calls and never the final answer',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const runtime=new SocialRuntime(ctx,store,testSettings(),async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9001'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(textToolResponse('我先查一下最近的记录','u1','qq_get_unread_messages',args),textResponse('这是最终答复，不会自动发送'));
  await runtime.accept(event('forward-1','group:1001',{atBot:true}));await settle(runtime);await pause(10);
  assert.equal(sent.length,1);
  assert.equal(sent[0].path,'/send');
  assert.equal(sent[0].body.message,'我先查一下最近的记录');
  assert.ok(sent[0].body.requestId.startsWith('progress:'));
  assert.equal(sent[0].body.triggerEventId,'forward-1');
  assert.ok(!JSON.stringify(sent).includes('最终答复'));
 }finally{await runtime.close();await ctx.fiber.dispose();}
});

test('forward mode keeps the per-turn cap',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressMaxForwardPerTurn:1},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9002'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(textToolResponse('第一条进度','u1','qq_get_unread_messages',args),textToolResponse('第二条进度','u2','qq_get_unread_messages',args),textResponse(''));
  await runtime.accept(event('forward-2','group:1001',{atBot:true}));await settle(runtime);await pause(10);
  assert.equal(sent.length,1);assert.equal(sent[0].body.message,'第一条进度');
 }finally{await runtime.close();await ctx.fiber.dispose();}
});

test('heartbeat mode reports counts on the configured interval and stops at turn end',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const gate=Promise.withResolvers();
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressModelsWithout:new Set(['script']),progressIntervalMs:20,progressMaxHeartbeatPerTurn:20},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9003'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(toolResponse('u1','qq_get_unread_messages',args),async()=>{await gate.promise;return textResponse('');});
  await runtime.accept(event('heartbeat-1','group:1001',{atBot:true}));
  await pause(150);
  const beats=sent.filter(item=>item.body.requestId.includes(':hb:'));
  assert.ok(beats.length>=1,'heartbeat should fire while the turn runs');
  assert.ok(beats.at(-1).body.message.startsWith('任务进行中（已'));
  assert.match(beats.at(-1).body.message,/运行了 1 条命令、0 次搜索、\d+ 次思考/);
  gate.resolve();await settle(runtime);await pause(40);
  const after=sent.length;await pause(60);
  assert.equal(sent.length,after,'no heartbeat after turn end');
  assert.equal(conv.timers.has('progress'),false);
 }finally{gate.resolve();await runtime.close();await ctx.fiber.dispose();}
});

test('progress reports can be switched off entirely',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressReports:'off',progressModelsWithout:new Set(['script']),progressIntervalMs:20},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9004'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(textToolResponse('不该被发送','u1','qq_get_unread_messages',args),textResponse(''));
  await runtime.accept(event('off-1','group:1001',{atBot:true}));await settle(runtime);await pause(60);
  assert.equal(sent.length,0);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});
