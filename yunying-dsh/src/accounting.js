// Read native lifecycle facts. No model invocation, Session storage or Agent loop lives here.
import { expandAssistantStream } from '@deepseek-ai/dsh-llm';
export class NativeAccounting {
  constructor(sessionId,key,config) {
    this.sessionId=sessionId;this.key=key;this.config=config;this.seen=new Set();this.tasks=new Map();this.turn=0;this.source=null;this.route={};this.floor=-1;this.replaying=true;
  }
  finishReplay(lastSeq) {this.floor=Math.max(this.floor,lastSeq??-1);this.seen.clear();this.replaying=false;}
  apply(event,latest) {
    if(event.seq<=this.floor||this.seen.has(event.seq))return;
    if(this.replaying)this.seen.add(event.seq);else this.floor=event.seq;
    const data=event.data;
    if(event.type==='turn/start')this.turn=data.turn;
    if(event.type==='request/header')this.route=data.header.config;
    if(event.type==='request/context')this.route=data;
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
    const record={record_kind:attempt?'attempt':'task-end',task_key:key,task_type:compact?'dsh_compaction':'social_turn',
      native_turn:data.turn??this.turn,native_step:data.step??null,native_event_seq:event.seq,
      group_id:(this.key.startsWith('private:')?'-':'')+this.key.split(':')[1],sender_id:source.userId||'',sender_name:source.sender||'',message_text:source.text||'',
      call_type:compact?(success?'compaction':'compaction_error'):success?'agent':'agent_error',
      model:message.source?.model||data.model||this.route.model||this.config.model,provider:message.source?.provider||data.provider||this.route.provider||this.config.provider,
      system_prompt:'[DSH native Session owns system prompt and compaction history]',user_prompt:source.plain||source.text||'',
      response_content:content.filter(c=>c.type==='text').map(c=>c.text).join('\n'),
      tool_calls:content.filter(c=>c.type==='tool-call').map(c=>({name:c.name,id:c.id})),token_usage:usage??null,
      started_at:stream[0]?.time??task.started,ended_at:event.time,
      duration_ms:Math.max(0,(stream.at(-1)?.time??event.time)-(stream[0]?.time??task.started)),
      success:!!success,error_message:success?'':data.interrupted?'DSH model stream interrupted':attempt?'DSH model attempt failed':'',
      ...(attempt?{attempt_no:++task.attempts}:{}),
      ...(terminal?{task_status:reason==='completed'?'success':reason==='aborted'?'cancelled':'failed',end_reason:reason||'unknown'}:{})};
    if(terminal)this.tasks.delete(key);
    return record;
  }
}
