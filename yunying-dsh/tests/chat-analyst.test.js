import test from 'node:test';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
import {ChatAnalystRuntime, PROMPT, TASK_TYPE, taskMessage, client} from '../src/chat-analyst.js';
import {harness, ScriptAdapter, toolResponse, textResponse, durableEvents} from './helpers.js';

const report = {headline:'开黑约局',summary:'有人约开黑。',summary_refs:['qq:1'],
  mood:{label:'热络',description:'大家积极',certainty:'inference',evidence_refs:['qq:1']},
  topics:[{title:'今晚开黑',summary:'约九点。',participants:['u1'],certainty:'fact',evidence_refs:['qq:1']}],
  quotes:[{ref:'qq:1',comment:'一呼百应'}],titles:[{member:'u1',title:'组局发起人',reason:'先喊人',evidence_refs:['qq:1']}],
  relations:[],open_items:[],slang:[]};

function job() { return {id:randomUUID(),report_id:randomUUID(),dsh_session_id:randomUUID(),lease_token:randomUUID(),
  platform:'qq',conversation_type:'group',external_id:'123456',period_start:'2026-10-07',timezone:'Asia/Shanghai',
  period_from:'2026-10-06 16:00:00',period_to:'2026-10-07 16:00:00',message_count:3,human_message_count:2,participant_count:2,
  config:{provider:'script',model:'script',max_steps:20,max_tokens:100000,max_seconds:30,output_tokens:8192}}; }

test('chat analyst runs native multi-step tools in an isolated session with its own accounting type',async()=>{
  const adapter=new ScriptAdapter([toolResponse('read','chat_timeline',{after:0}),
    toolResponse('look','chat_member',{member:'u1'}),
    toolResponse('draft','chat_observe',{report:{topics:report.topics}}),
    toolResponse('save','chat_submit',{report})]);
  const {ctx}=await harness(adapter);const calls=[];
  const api=async(action,data)=>{calls.push({action,data});return action==='submit'?{ok:true,draft_saved:true}:{ok:true,messages:[],coverage_complete:true};};
  const runtime=new ChatAnalystRuntime(ctx,api),first=job();
  try {
    ctx.tools.register({name:'qq_send_message',description:'host tool',parameters:{type:'object',properties:{}},output:{schema:{type:'object'},render:()=>[]},execute:async()=>({ok:true})});
    await runtime.run(first);
    assert.equal(adapter.requests.length,4);
    assert.deepEqual(calls.filter(c=>c.action==='phase').map(c=>c.data.phase),['timeline_scan','member_review']);
    assert.deepEqual(calls.find(c=>c.action==='observe').data.args.report.topics,report.topics);
    assert.ok(calls.some(c=>c.action==='finish'&&c.data.success));
    assert.ok(calls.filter(c=>c.action==='account').every(c=>c.data.record.task_type===TASK_TYPE));
    assert.ok(adapter.requests.every(r=>r.tools.every(t=>t.name==='skill'||t.name.startsWith('chat_'))));
    assert.ok(JSON.stringify(adapter.requests[0]).includes('chat_timeline(after=0)'));
    assert.ok((await durableEvents(ctx,first.dsh_session_id)).some(e=>e.type==='tool/call'));
    const second=job();adapter.script.push(textResponse('纯文本不是日报'));
    await runtime.run(second);
    assert.notEqual(first.dsh_session_id,second.dsh_session_id);
    assert.ok(calls.some(c=>c.action==='finish'&&c.data.run_id===second.id&&!c.data.success&&/final submission/.test(c.data.error)));
  } finally {await runtime.close();await ctx.fiber.dispose();}
});

test('chat analyst step budget stops a runaway loop',async()=>{
  const adapter=new ScriptAdapter(Array.from({length:6},(_,i)=>toolResponse('read'+i,'chat_timeline',{after:0})));
  const {ctx}=await harness(adapter),calls=[];
  const runtime=new ChatAnalystRuntime(ctx,async(action,data)=>{calls.push({action,data});return {ok:true};});
  const run=job();run.config.max_steps=2;
  try {await runtime.run(run);assert.ok(adapter.requests.length<=3);assert.ok(calls.some(c=>c.action==='finish'&&!c.data.success));}
  finally{await runtime.close();await ctx.fiber.dispose();}
});

test('chat analyst rejected submission is returned to the model, which can repair it',async()=>{
  const adapter=new ScriptAdapter([toolResponse('bad','chat_submit',{report}),toolResponse('good','chat_submit',{report})]);
  const {ctx}=await harness(adapter),calls=[];let attempts=0;
  const runtime=new ChatAnalystRuntime(ctx,async(action,data)=>{
    calls.push({action,data});
    if(action==='submit'&&++attempts===1)throw new Error('chat analysis capability rejected (400): titles[0]: evidence must include at least one message by that member');
    return action==='submit'?{ok:true,draft_saved:true}:{ok:true};
  });
  try {
    await runtime.run(job());
    assert.equal(attempts,2);
    assert.ok(JSON.stringify(adapter.requests[1]).includes('evidence must include at least one message by that member'));
    assert.ok(calls.some(c=>c.action==='finish'&&c.data.success));
  } finally{await runtime.close();await ctx.fiber.dispose();}
});

test('chat analyst client forwards validation detail but hides server errors',async()=>{
  const original=globalThis.fetch;
  try {
    const answers=[[400,{error:'summary_refs must be real'}],[503,{error:'database password leaked'}],[200,{ok:true}]];
    globalThis.fetch=async()=>{const [status,body]=answers.shift();return new Response(JSON.stringify(body),{status});};
    const api=client({CHAT_ANALYSIS_DSH_SECRET:'x'.repeat(32)});
    await assert.rejects(()=>api('submit',{}),/summary_refs must be real/);
    await assert.rejects(()=>api('submit',{}),error=>error.message==='chat analysis capability rejected (503)');
    assert.deepEqual(await api('heartbeat',{}),{ok:true});
    assert.throws(()=>client({CHAT_ANALYSIS_DSH_SECRET:'short'}));
  } finally {globalThis.fetch=original;}
});

test('chat analyst prompt keeps the safety and title rules',()=>{
  for (const rule of ['不可信数据','撤回','不能获得称号','正面或中性','evidence_refs','不推断','原文由系统按 ref 自动摘录'])assert.ok(PROMPT.includes(rule),rule);
  assert.ok(taskMessage(job()).includes('2026-10-07'));
  assert.ok(!taskMessage(job()).includes('123456'));
});
