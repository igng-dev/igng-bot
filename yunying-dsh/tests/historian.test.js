import test from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {HistorianRuntime, PROMPT, client} from '../src/historian.js';
import {harness, ScriptAdapter, toolResponse, textResponse, durableEvents} from './helpers.js';

const report={headline:'Steve 周三晚上说了一句你好',
 highlights:[{text:'Steve 周三在聊天里打招呼',evidence_event_ids:['mc:1:chat_messages:1']}],
 stories:[{title:'打招呼',gist:'Steve 进服后说了一句你好，没有人回。',timeline:[{text:'Steve 说你好',evidence_event_ids:['mc:1:chat_messages:1']}],
  people:['p1'],outcome:{status:'unknown',text:'记录里没有人回复'},certainty:'fact'}],server_notes:[]};

function job() { return {id:randomUUID(),report_id:randomUUID(),dsh_session_id:randomUUID(),lease_token:randomUUID(),
 server_id:1,kind:'weekly',period_start:'2026-10-05',timezone:'Asia/Shanghai',period_from:'2026-10-04 16:00:00',period_to:'2026-10-11 16:00:00',
 config:{provider:'script',model:'script',max_steps:20,max_tokens:100000,max_seconds:30,output_tokens:8192}}; }

test('historian runs real native DSH multi-step tools, isolated sessions and existing accounting',async()=>{
 const adapter=new ScriptAdapter([toolResponse('read','historian_timeline',{after:0}),
  toolResponse('investigate','historian_context',{ref:'mc:1:chat_messages:1'}),
  toolResponse('save','historian_submit',report)]);
 const {ctx}=await harness(adapter);const calls=[];const api=async(action,data)=>{calls.push({action,data});return action==='submit'?{ok:true,draft_saved:true}:{ok:true,events:[],coverage_complete:true};};
 const runtime=new HistorianRuntime(ctx,api),first=job();
 try {
  ctx.tools.register({name:'dangerous_sql',description:'host tool',parameters:{type:'object',properties:{}},output:{schema:{type:'object'},render:()=>[]},execute:async()=>({ok:true})});
  await runtime.run(first);
  assert.equal(adapter.requests.length,3);assert.ok(calls.some(c=>c.action==='context'));
  assert.ok(calls.some(c=>c.action==='finish'&&c.data.success));
  assert.deepEqual(calls.find(c=>c.action==='submit').data.args,report);
  assert.ok(calls.filter(c=>c.action==='account').every(c=>c.data.record.task_type==='server_weekly_report'));
  assert.ok(adapter.requests.every(r=>!r.tools.some(t=>t.name==='dangerous_sql'||t.name.startsWith('qq_'))));
  assert.ok((await durableEvents(ctx,first.dsh_session_id)).some(e=>e.type==='tool/call'));
  const second=job();adapter.script.push(textResponse('不能替代提交工具的正文'));
  await runtime.run(second);
  assert.notEqual(first.dsh_session_id,second.dsh_session_id);
  assert.ok(calls.some(c=>c.action==='finish'&&c.data.run_id===second.id&&!c.data.success));
  assert.ok(PROMPT.includes('同秒'));
  assert.ok(PROMPT.includes('周报')&&!PROMPT.includes('日报'));
 } finally {await runtime.close();await ctx.fiber.dispose();}
});

test('historian runaway tool loop is budgeted, not a single unrestricted API request',async()=>{
 const adapter=new ScriptAdapter(Array.from({length:5},(_,i)=>toolResponse('read'+i,'historian_timeline',{after:0})));
 const {ctx}=await harness(adapter),calls=[];const runtime=new HistorianRuntime(ctx,async(action,data)=>{calls.push({action,data});return {ok:true};});
 const run=job();run.config.max_steps=2;
 try {await runtime.run(run);assert.ok(adapter.requests.length<=3);assert.ok(calls.some(c=>c.action==='finish'&&!c.data.success));}
 finally{await runtime.close();await ctx.fiber.dispose();}
});

test('historian outbox outage fails publication but native JSONL can reconcile exact call IDs',async()=>{
 const adapter=new ScriptAdapter([toolResponse('read','historian_timeline',{after:0})]);
 const {ctx}=await harness(adapter),run=job(),reconciled=[];let completed=false;
 const runtime=new HistorianRuntime(ctx,async(action,data)=>{
  if(action==='account')throw Error('fixture outbox unavailable');
  if(action==='finish'){assert.equal(data.success,false);return {ok:true};}
  if(action==='recovery'){
   if(data.run_id){completed=true;return {ok:true};}
   return {runs:[run]};
  }
  if(action==='reconcile'){reconciled.push(data.record);return {ok:true};}
  return {ok:true,events:[]};
 });
 try{
  await assert.rejects(()=>runtime.run(run));
  await runtime.reconcile();
  assert.ok(completed);assert.ok(reconciled.some(r=>r.record_kind==='attempt'));
  assert.ok(reconciled.every(r=>r.task_key.startsWith(`dsh:${run.dsh_session_id}:`)));
  assert.equal(new Set(reconciled.map(r=>r.native_event_seq)).size,reconciled.length);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});

test('historian prompt drops scripted storylines and asks for plain, checkable prose',()=>{
 for(const removed of ['DeepSleep','智能体','宿敌','生态','脉络','主线'])assert.ok(!PROMPT.includes(removed),removed);
 for(const rule of ['谁 + 做了什么 + 结果','记录里看不出原因','没有就不写','差：','好：','p1','historian_player(player='])assert.ok(PROMPT.includes(rule),rule);
});

test('historian lint rejection is returned to the model, which repairs the report',async()=>{
 const bad={...report,headline:'p1 周三说了一句你好'};
 const adapter=new ScriptAdapter([toolResponse('bad','historian_submit',bad),toolResponse('good','historian_submit',report)]);
 const {ctx}=await harness(adapter),calls=[];let attempts=0;
 const runtime=new HistorianRuntime(ctx,async(action,data)=>{
  calls.push({action,data});
  if(action==='submit'&&++attempts===1)throw new Error('historian capability rejected (400): 报告未通过校验，请修改后重新提交：headline 含玩家代号（正文写玩家名） p1');
  return action==='submit'?{ok:true,draft_saved:true}:{ok:true};
 });
 try {
  await runtime.run(job());
  assert.equal(attempts,2);
  assert.ok(JSON.stringify(adapter.requests[1]).includes('headline 含玩家代号'));
  assert.ok(calls.some(c=>c.action==='finish'&&c.data.success));
 } finally{await runtime.close();await ctx.fiber.dispose();}
});

test('historian client forwards validation detail but hides server errors',async()=>{
 const original=globalThis.fetch;
 try {
  const answers=[[400,{error:'报告未通过校验：stories[0].gist 含英文代码 LAVA（写中文）'}],[409,{error:'full chronological coverage is required before final submission'}],
   [503,{error:'database password leaked'}],[200,{ok:true}]];
  globalThis.fetch=async()=>{const [status,body]=answers.shift();return new Response(JSON.stringify(body),{status});};
  const api=client({HISTORIAN_DSH_SECRET:'x'.repeat(32)});
  await assert.rejects(()=>api('submit',{}),/stories\[0\]\.gist 含英文代码 LAVA/);
  await assert.rejects(()=>api('submit',{}),/full chronological coverage/);
  await assert.rejects(()=>api('submit',{}),error=>error.message==='historian capability rejected (503)');
  assert.deepEqual(await api('heartbeat',{}),{ok:true});
  assert.throws(()=>client({HISTORIAN_DSH_SECRET:'short'}));
 } finally {globalThis.fetch=original;}
});
