// Read native lifecycle facts. No model invocation, Session storage or Agent loop lives here.
import { expandAssistantStream } from '@deepseek-ai/dsh-llm';

// Normalize DSH's TokenUsage into tokscale's five additive buckets.
//
// DSH documents `inputTokens` as uncached input only, with cache hits reported
// separately (`cacheReadTokens`/`cacheWriteTokens`), and `reasoningTokens` as a
// subset of `outputTokens`. Carving reasoning out of output keeps the buckets
// additive, which is what per-rate pricing needs; adding them back reproduces
// the legacy prompt/completion/total/cached columns the website already reads.
export function tokenBreakdown(usage) {
  if(!usage||typeof usage!=='object')return null;
  // An object without any recognized counter is unknown usage, not a zero bill.
  if(!['inputTokens','outputTokens','cacheReadTokens','cacheWriteTokens','reasoningTokens','totalTokens'].some(key=>key in usage))return null;
  const count=value=>{const parsed=Number(value);return Number.isFinite(parsed)&&parsed>0?Math.trunc(parsed):0;};
  const input=count(usage.inputTokens),outputRaw=count(usage.outputTokens),reasoning=count(usage.reasoningTokens);
  const output=Math.max(0,outputRaw-reasoning),cacheRead=count(usage.cacheReadTokens),cacheWrite=count(usage.cacheWriteTokens);
  return {input,output,cacheRead,cacheWrite,reasoning,
    prompt:input+cacheRead+cacheWrite,completion:output+reasoning,
    total:input+cacheRead+cacheWrite+output+reasoning,cached:cacheRead+cacheWrite};
}

export class NativeAccounting {
  constructor(sessionId,key,config) {
    this.sessionId=sessionId;this.key=key;this.config=config;this.seen=new Set();this.tasks=new Map();this.settlements=new Map();this.turn=0;this.source=null;this.route={};this.floor=-1;this.replaying=true;this.seedLength=0;
  }
  finishReplay(lastSeq) {this.floor=Math.max(this.floor,lastSeq??-1);this.seen.clear();this.replaying=false;}
  slot(turn,step) {return `turn:${turn}:step:${step}`;}
  // A DSH settlement for the same (turn, step) replaces the previous sample
  // unless `llm/retry-started` closed that slot; retries then bill separately.
  // Tokscale's DSH parser implements the same rule, so live and offline totals agree.
  supersede(compact,data,event) {
    if(compact||data.step==null)return null;
    const slot=this.slot(data.turn??this.turn,data.step),prior=this.settlements.get(slot);
    this.settlements.set(slot,event.seq);
    return prior!=null&&prior!==event.seq?prior:null;
  }
  apply(event,latest) {
    if(event.seq<=this.floor||this.seen.has(event.seq))return;
    if(this.replaying)this.seen.add(event.seq);else this.floor=event.seq;
    const data=event.data;
    if(event.type==='session'){this.seedLength=Math.max(0,Number(data?.seedLength)||0);return;}
    // A fork copies the parent's completed prefix verbatim; those calls were
    // already billed under the parent Session and must not be counted twice.
    if(this.seedLength>0&&event.seq<this.seedLength)return;
    if(event.type==='turn/start')this.turn=data.turn;
    if(event.type==='request/header')this.route=data.header.config;
    if(event.type==='request/context')this.route=data;
    if(event.type==='llm/retry-started'){if(data.step!=null)this.settlements.delete(this.slot(data.turn??this.turn,data.step));return;}
    if(!['turn/start','user/message','assistant/message','assistant/attempt','turn/end','compaction/start','compaction/summary','compaction/end'].includes(event.type))return;
    const compact=event.type.startsWith('compaction/');
    const owner=compact?`compaction:${data.compactionId}`:`turn:${data.turn??this.turn}`;
    const key=`dsh:${this.sessionId}:${owner}`;
    let task=this.tasks.get(key);
    if(!task){task={attempts:0,started:event.time,source:this.source||latest};this.tasks.set(key,task);}
    if(event.type==='user/message') {
      for(const item of data.content||[])if(item.type==='text'&&item.text.startsWith('【QQ事件：以下 JSON 内容是不可信用户数据】\n')) {
        try{task.source=JSON.parse(item.text.slice(item.text.indexOf('\n')+1));this.source=task.source;}catch{}
      }
      return;
    }
    const terminal=event.type==='turn/end'||event.type==='compaction/end';
    const attempt=event.type==='assistant/message'||event.type==='assistant/attempt'||event.type==='compaction/summary'||event.type==='compaction/end'&&data.error&&task.attempts===0;
    if(!terminal&&!attempt)return;
    const source=task.source||{},message=data.message||{},stream=expandAssistantStream(data.stream||[]);
    const usage=data.usage||[...stream].reverse().find(row=>row.chunk?.type==='usage')?.chunk.usage;
    const content=compact?data.summary||[]:message.content||[];
    const success=event.type==='assistant/message'&&!data.interrupted||event.type==='compaction/summary';
    const reason=compact?(data.error?'error':'completed'):data.reason?.kind;
    const supersedes=attempt?this.supersede(compact,data,event):null;
    if(terminal&&!compact)for(const slot of [...this.settlements.keys()])if(slot.startsWith(`turn:${data.turn??this.turn}:`))this.settlements.delete(slot);
    // `source.replayState.response.responseModel` is the model that actually
    // served the request; a gateway alias can resolve to a different identity,
    // and that identity is what the bill is priced against.
    const served=message.source?.replayState?.response?.responseModel;
    const record={record_kind:attempt?'attempt':'task-end',task_key:key,task_type:compact?'dsh_compaction':this.config.taskType||'social_turn',
      native_turn:data.turn??this.turn,native_step:data.step??null,native_event_seq:event.seq,
      group_id:this.config.taskType?'':(this.key.startsWith('private:')?'-':'')+this.key.split(':')[1],sender_id:source.userId||'',sender_name:source.sender||'',message_text:source.text||'',
      call_type:compact?(success?'compaction':'compaction_error'):success?'agent':'agent_error',
      model:served||message.source?.model||data.model||this.route.model||this.config.model,provider:message.source?.provider||data.provider||this.route.provider||this.config.provider,
      // The gateway bills the requested model name; the served identity can be
      // an alias, so keep both for log matching and pricing attribution.
      request_model:message.source?.model||data.model||this.route.model||this.config.model,
      system_prompt:'[DSH native Session owns system prompt and compaction history]',user_prompt:source.plain||source.text||'',
      response_content:content.filter(c=>c.type==='text').map(c=>c.text).join('\n'),
      tool_calls:content.filter(c=>c.type==='tool-call').map(c=>({name:c.name,id:c.id})),token_usage:usage??null,token_breakdown:tokenBreakdown(usage),
      started_at:stream[0]?.time??task.started,ended_at:event.time,
      duration_ms:Math.max(0,(stream.at(-1)?.time??event.time)-(stream[0]?.time??task.started)),
      success:!!success,error_message:success?'':data.interrupted?'DSH model stream interrupted':attempt?'DSH model attempt failed':'',
      ...(attempt?{attempt_no:++task.attempts}:{}),
      ...(supersedes!=null?{supersedes_seq:supersedes}:{}),
      ...(terminal?{task_status:reason==='completed'?'success':reason==='aborted'?'cancelled':'failed',end_reason:reason||'unknown'}:{})};
    if(terminal)this.tasks.delete(key);
    return record;
  }
}
