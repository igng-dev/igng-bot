import test from 'node:test';import assert from 'node:assert/strict';
import {NativeAccounting,tokenBreakdown} from '../src/accounting.js';

const accounting=()=>new NativeAccounting('session-1','group:1001',{model:'config-model',provider:'config-provider'});
const message=(seq,{turn=1,step=1,usage,stream,source,interrupted}={})=>({seq,time:1000+seq,type:'assistant/message',
  data:{turn,step,message:{id:`m-${seq}`,source:source??{kind:'model',provider:'newapi',model:'cc/deepseek/deepseek-v4.1-flash'},content:[{type:'text',text:'ok'}]},usage,stream,...(interrupted?{interrupted:true}:{})}});

test('token breakdown carves reasoning out of output and keeps buckets additive',()=>{
  const breakdown=tokenBreakdown({inputTokens:30,outputTokens:10,cacheReadTokens:20,cacheWriteTokens:5,reasoningTokens:4});
  assert.deepEqual(breakdown,{input:30,output:6,cacheRead:20,cacheWrite:5,reasoning:4,
    prompt:55,completion:10,total:65,cached:25});
  assert.equal(tokenBreakdown(null),null);
  assert.equal(tokenBreakdown({}),null);
});

test('native accounting emits tokscale buckets and the legacy columns together',()=>{
  const acct=accounting();
  const record=acct.apply(message(1,{usage:{inputTokens:30,outputTokens:10,cacheReadTokens:20,cacheWriteTokens:5,reasoningTokens:4}}));
  assert.equal(record.record_kind,'attempt');
  assert.equal(record.token_usage.reasoningTokens,4);
  assert.deepEqual(record.token_breakdown,{input:30,output:6,cacheRead:20,cacheWrite:5,reasoning:4,
    prompt:55,completion:10,total:65,cached:25});
});

test('a later settlement for the same turn/step supersedes the earlier attempt',()=>{
  const acct=accounting();
  const first=acct.apply(message(1,{usage:{inputTokens:10,outputTokens:5}}));
  assert.equal(first.supersedes_seq,undefined);
  const second=acct.apply(message(2,{usage:{inputTokens:12,outputTokens:6}}));
  assert.equal(second.supersedes_seq,1);
});

test('llm/retry-started closes the settlement slot so retries bill separately',()=>{
  const acct=accounting();
  acct.apply(message(1,{usage:{inputTokens:10,outputTokens:5}}));
  acct.apply({seq:2,time:1002,type:'llm/retry-started',data:{turn:1,step:1}});
  const retry=acct.apply(message(3,{usage:{inputTokens:12,outputTokens:6}}));
  assert.equal(retry.supersedes_seq,undefined);
});

test('a different step is a separate attempt, not a replacement',()=>{
  const acct=accounting();
  acct.apply(message(1,{step:1,usage:{inputTokens:10,outputTokens:5}}));
  const next=acct.apply(message(2,{step:2,usage:{inputTokens:12,outputTokens:6}}));
  assert.equal(next.supersedes_seq,undefined);
});

test('compaction summaries are separate billed calls',()=>{
  const acct=accounting();
  const record=acct.apply({seq:1,time:1001,type:'compaction/summary',data:{compactionId:'cmp-1',summary:[{type:'text',text:'s'}],usage:{inputTokens:7,outputTokens:3}}});
  assert.equal(record.record_kind,'attempt');
  assert.equal(record.task_type,'dsh_compaction');
  assert.equal(record.call_type,'compaction');
  assert.equal(record.supersedes_seq,undefined);
});

test('the served response model wins over the configured alias',()=>{
  const acct=accounting();
  const record=acct.apply(message(1,{usage:{inputTokens:1,outputTokens:1},source:{kind:'model',provider:'newapi',model:'cc/alias',replayState:{response:{responseModel:'served-identity'}}}}));
  assert.equal(record.model,'served-identity');
  assert.equal(record.request_model,'cc/alias');
  assert.equal(record.provider,'newapi');
});

test('a fork seed prefix is skipped and only this session own work is billed',()=>{
  const acct=accounting();
  assert.equal(acct.apply({seq:0,time:1000,type:'session',data:{seedLength:5}}),undefined);
  assert.equal(acct.apply(message(3,{usage:{inputTokens:9,outputTokens:9}})),undefined);
  const own=acct.apply(message(6,{usage:{inputTokens:10,outputTokens:5}}));
  assert.equal(own.record_kind,'attempt');
  assert.equal(own.native_event_seq,6);
});

test('replayed events below the floor are not billed twice after restore',()=>{
  const acct=accounting();
  acct.apply(message(1,{usage:{inputTokens:10,outputTokens:5}}));
  acct.finishReplay(1);
  assert.equal(acct.apply(message(1,{usage:{inputTokens:10,outputTokens:5}})),undefined);
  assert.equal(acct.apply(message(2,{usage:{inputTokens:11,outputTokens:6}})).native_event_seq,2);
});
