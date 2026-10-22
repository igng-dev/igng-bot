import test from 'node:test';
import assert from 'node:assert/strict';
import { SocialRuntime } from '../src/runtime.js';
import { PROGRESS_NOTE_TOOL, progressMode, progressNoteEnabled, progressNoteText, heartbeatAllowed, heartbeatText, forwardableText, emptyProgress, countToolCall, isProgressNoteTool, isReplyTool, isSubstantiveTool } from '../src/progress.js';
import { FixtureStore,harness,ScriptAdapter,testSettings,textResponse,toolResponse,textToolResponse,event,settle } from './helpers.js';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('progress helpers: mode selection, bounded text and heartbeat wording',()=>{
 assert.equal(progressMode({progressReports:'auto',progressModelsWithout:new Set(['silent']),model:'silent'}),'heartbeat');
 assert.equal(progressMode({progressReports:'auto',progressModelsWithout:new Set(),model:'deepseek-v4-flash'}),'forward');
 assert.equal(progressMode({progressReports:'off',progressModelsWithout:new Set(['silent']),model:'silent'}),'off');
 assert.equal(progressNoteEnabled({progressReports:'auto',progressModelsWithout:new Set(['silent']),model:'silent'}),true);
 assert.equal(progressNoteEnabled({progressReports:'auto',progressModelsWithout:new Set(),model:'deepseek-v4-flash'}),false);
 assert.equal(progressNoteEnabled({progressReports:'off',progressModelsWithout:new Set(['silent']),model:'silent'}),false);
 // Reasoning is not process output and blank text is not worth a message.
 assert.equal(forwardableText([{type:'reasoning',text:'内部推理不算'},{type:'text',text:'  '}]),null);
 assert.equal(forwardableText([{type:'text',text:'查一下最近的记录'}]),'查一下最近的记录');
 const cut=forwardableText([{type:'text',text:'字'.repeat(500)}]);
 assert.equal(cut.length,400);assert.ok(cut.endsWith('…'));
 assert.equal(progressNoteText('  我查一下这个  '),'我查一下这个');
 assert.equal(progressNoteText('  \n '),null);
 const longNote=progressNoteText('字'.repeat(200));
 assert.equal(longNote.length,80);assert.ok(longNote.endsWith('…'));
 assert.equal(isProgressNoteTool(PROGRESS_NOTE_TOOL),true);
 assert.equal(isProgressNoteTool('web_search'),false);
 const progress=emptyProgress(3,1000);
 assert.equal(progress.substantive,false);
 assert.equal(progress.notes,0);
 assert.equal(heartbeatText(progress,31000),'还在处理（已 30 秒），稍等。');
 assert.equal(heartbeatAllowed(progress,{progressNoteHoldMs:30000},1000),true);
 progress.notes=1;progress.lastNoteAt=1000;
 assert.equal(heartbeatAllowed(progress,{progressNoteHoldMs:30000},10000),false);
 assert.equal(heartbeatAllowed(progress,{progressNoteHoldMs:30000},32000),true);
 countToolCall(progress,'qq_get_unread_messages');
 assert.equal(progress.substantive,false,'social bookkeeping is not announced');
 countToolCall(progress,'web_search');
 assert.equal(progress.substantive,true);
 assert.equal(heartbeatText(progress,31000),'正在查资料（已 30 秒）：搜了 1 次。');
 assert.equal(isSubstantiveTool('qq_video_open'),true);
 assert.equal(isSubstantiveTool('qq_get_unread_messages'),false);
 assert.equal(isReplyTool('qq_send_message'),true);
 assert.equal(isReplyTool('qq_get_unread_messages'),false);
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

test('silent model progress_note sends user-facing text and holds the fallback heartbeat',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const gate=Promise.withResolvers();
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressModelsWithout:new Set(['script']),progressIntervalMs:20,progressMaxNotesPerTurn:2},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9010'};});
 try{
  const conv=await runtime.load('group:1001');
  conv.webSearch=async()=>({query:'今天天气',results:[],untrusted:true});
  const args={key:conv.state.key,token:conv.state.agentToken};
  const noteTool=conv.handle.agent.ctx.tools.schemas(conv.handle.agent).find(tool=>tool.name===PROGRESS_NOTE_TOOL);
  assert.ok(noteTool,'silent models receive the dedicated progress tool');
  const direct=await conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'note-direct',name:PROGRESS_NOTE_TOOL,arguments:{text:'直接测试'},signal:new AbortController().signal});
  assert.equal(direct.isError,false,'progress_note is local and does not need key/token');
  adapter.script.push(toolResponse('note-1',PROGRESS_NOTE_TOOL,{text:'我查一下这个'}),toolResponse('note-2',PROGRESS_NOTE_TOOL,{text:'第二条不该发'}),toolResponse('search-1','web_search',{...args,query:'今天天气'}),async()=>{await gate.promise;return textResponse('最终答复不会自动发送');});
  await runtime.accept(event('note-turn-1','group:1001',{atBot:true}));
  await pause(40);
  const notes=sent.filter(item=>item.body.requestId.includes(':note:'));
  assert.equal(notes.length,1,'the per-turn gap suppresses a second immediate note');
  assert.equal(notes[0].body.message,'我查一下这个');
  assert.ok(notes[0].body.requestId.startsWith('progress:'));
  const firstSystem=adapter.requests[0].messages.filter(message=>message.role==='system').flatMap(message=>message.content).filter(part=>part.type==='text').map(part=>part.text).join('\n');
  assert.ok(firstSystem.includes('【过程进度】'),'the silent-model prompt teaches when to call the tool');
  assert.equal(sent.filter(item=>item.body.requestId.includes(':hb:')).length,0,'a fresh note holds the generic heartbeat');
  assert.ok(!JSON.stringify(sent).includes('最终答复'));
  gate.resolve();await settle(runtime);await pause(20);
 }finally{gate.resolve();await runtime.close();await ctx.fiber.dispose();}
});

test('heartbeat stays quiet while a social turn only reads and replies',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const gate=Promise.withResolvers();
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressModelsWithout:new Set(['script']),progressIntervalMs:20,progressMaxHeartbeatPerTurn:20},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9003'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(toolResponse('u1','qq_get_unread_messages',args),async()=>{await gate.promise;return textResponse('');});
  await runtime.accept(event('heartbeat-social','group:1001',{atBot:true,text:'你在吗',plain:'你在吗'}));
  await pause(150);
  assert.equal(sent.filter(item=>item.body.requestId.includes(':hb:')).length,0,'reading unread is not announced');
  gate.resolve();await settle(runtime);await pause(40);
  assert.equal(conv.timers.has('progress'),false);
 }finally{gate.resolve();await runtime.close();await ctx.fiber.dispose();}
});

test('heartbeat reports a running search and stops once the reply goes out',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const gate=Promise.withResolvers();
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressModelsWithout:new Set(['script']),progressIntervalMs:20,progressMaxHeartbeatPerTurn:20},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9006'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(toolResponse('s1','web_search',{...args,query:'今天天气'}),async()=>{await gate.promise;return textResponse('');});
  await runtime.accept(event('heartbeat-search','group:1001',{atBot:true}));
  await pause(150);
  const beats=sent.filter(item=>item.body.requestId.includes(':hb:'));
  assert.ok(beats.length>=1,'a search still running is worth a heartbeat');
  assert.match(beats.at(-1).body.message,/正在查资料（已 \d+ 秒）：搜了 1 次。/);
  gate.resolve();await settle(runtime);await pause(40);
  const after=sent.length;await pause(60);
  assert.equal(sent.length,after,'no heartbeat after turn end');
  sent.length=0;
  const sentGate=Promise.withResolvers();
  adapter.script.push(toolResponse('s2','web_search',{...args,query:'明天'}),toolResponse('r1','qq_send_message',{...args,messages:'查到了'}),async()=>{await sentGate.promise;return textResponse('');});
  await runtime.accept(event('heartbeat-replied','group:1001',{atBot:true}));
  for(let n=0;n<100&&!conv.progress?.quiet;n++)await pause(10);
  assert.equal(conv.progress?.quiet,true,'the reply tool is visible before judging later heartbeats');
  const answered=sent.filter(item=>item.body.requestId.includes(':hb:')).length;
  await pause(80);
  assert.equal(sent.filter(item=>item.body.requestId.includes(':hb:')).length,answered,'answering ends the heartbeat even while the turn continues');
  sentGate.resolve();await settle(runtime);
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

test('heartbeat mode suppresses sending while conversation is in waiting state',async()=>{
 const {ctx,adapter}=await harness();const store=new FixtureStore(),sent=[];
 const runtime=new SocialRuntime(ctx,store,{...testSettings(),progressModelsWithout:new Set(['script']),progressIntervalMs:20,progressMaxHeartbeatPerTurn:20,minQuietMs:10,waitMinMs:10,preSleepWaitMs:10},async(path,body)=>{sent.push({path,body});return {ok:true,message_id:'9005'};});
 try{
  const conv=await runtime.load('group:1001');
  const args={key:conv.state.key,token:conv.state.agentToken};
  adapter.script.push(toolResponse('w1','web_search',{...args,query:'天气'}),toolResponse('w2','qq_wait_for_messages',{...args,timeoutMs:60,purpose:'messages'}),textResponse(''));
  await runtime.accept(event('heartbeat-wait-1','group:1001',{atBot:true}));
  for(let n=0;n<200&&!conv.state.waiting;n++)await pause(10);
  assert.equal(conv.state.waiting,true);
  const during=sent.filter(item=>item.body.requestId.includes(':hb:')).length;
  await pause(30);
  assert.equal(sent.filter(item=>item.body.requestId.includes(':hb:')).length,during,'heartbeat must not fire while waiting is true');
  await settle(runtime);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});
