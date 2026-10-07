import test from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {HistorianRuntime, PROMPT} from '../src/historian.js';
import {harness, ScriptAdapter, toolResponse, textResponse, durableEvents} from './helpers.js';

function job() { return {id:randomUUID(),report_id:randomUUID(),dsh_session_id:randomUUID(),lease_token:randomUUID(),
 server_id:1,kind:'daily',period_start:'2026-10-07',timezone:'Asia/Shanghai',period_from:'2026-10-06 16:00:00',period_to:'2026-10-07 16:00:00',
 config:{provider:'script',model:'script',max_steps:20,max_tokens:100000,max_seconds:30,output_tokens:8192}}; }

test('historian runs real native DSH multi-step tools, isolated sessions and existing accounting',async()=>{
 const adapter=new ScriptAdapter([toolResponse('read','historian_timeline',{after:0}),
  toolResponse('investigate','historian_context',{event_id:'mc:1:chat_messages:1'}),
  toolResponse('save','historian_submit',{observations:[{title:'互动',summary:'玩家说你好',certainty:'fact',evidence_event_ids:['mc:1:chat_messages:1']}]})]);
 const {ctx}=await harness(adapter);const calls=[];const api=async(action,data)=>{calls.push({action,data});return action==='submit'?{ok:true,draft_saved:true}:{ok:true,events:[],coverage_complete:true};};
 const runtime=new HistorianRuntime(ctx,api),first=job();
 try {
  ctx.tools.register({name:'dangerous_sql',description:'host tool',parameters:{type:'object',properties:{}},output:{schema:{type:'object'},render:()=>[]},execute:async()=>({ok:true})});
  await runtime.run(first);
  assert.equal(adapter.requests.length,3);assert.ok(calls.some(c=>c.action==='context'));
  assert.ok(calls.some(c=>c.action==='finish'&&c.data.success));
  assert.ok(calls.filter(c=>c.action==='account').every(c=>c.data.record.task_type==='server_daily_report'));
  assert.ok(adapter.requests.every(r=>!r.tools.some(t=>t.name==='dangerous_sql'||t.name.startsWith('qq_'))));
  assert.ok((await durableEvents(ctx,first.dsh_session_id)).some(e=>e.type==='tool/call'));
  const second=job();adapter.script.push(textResponse('不能替代提交工具的正文'));
  await runtime.run(second);
  assert.notEqual(first.dsh_session_id,second.dsh_session_id);
  assert.ok(calls.some(c=>c.action==='finish'&&c.data.run_id===second.id&&!c.data.success));
  assert.ok(PROMPT.includes('同秒'));
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
