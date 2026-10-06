import { Context } from '@deepseek-ai/cordis';
import LlmRuntime,{LlmAdapter} from '@deepseek-ai/dsh-llm';
import SessionStore from '@deepseek-ai/dsh-session';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import Skills from '@deepseek-ai/dsh-skill';
import * as ToolSkill from '@deepseek-ai/dsh-tool-skill';
import JsonlPersistence from '@deepseek-ai/dsh-session-persistence-jsonl';
import Attachments from '@deepseek-ai/dsh-attachment-local';
import TokenMeter from '@deepseek-ai/dsh-token-meter';
import Compaction from '@deepseek-ai/dsh-compaction-basic';
import { randomUUID } from 'node:crypto';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { settings } from '../src/policy.js';
export const testSettings=()=>({...settings({YUNYING_INTERNAL_SECRET:randomUUID(),YUNYING_ALLOW_GROUPS:'1001,1002',YUNYING_ALLOW_PRIVATE:'2001',YUNYING_MODEL_PROVIDER:'script',YUNYING_MODEL:'script'}),batchWindowMs:1,replyCheckMs:3600000,proactiveMinMs:3600000,proactiveMaxMs:3600000});
export const textResponse=text=>[
 {type:'block-start',index:0,blockType:'text'}, {type:'block-end',index:0,block:{type:'text',text}},
 {type:'usage',usage:{inputTokens:10,outputTokens:Math.max(1,text.length)}},{type:'finish',reason:{kind:'stop'}}];
export const toolResponse=(id,name,args)=>[
 {type:'block-start',index:0,blockType:'tool-call'},
 {type:'tool-call-delta',index:0,id,name,argumentsDelta:JSON.stringify(args)},
 {type:'block-end',index:0,block:{type:'tool-call',id,name,arguments:JSON.stringify(args)}},
 {type:'usage',usage:{inputTokens:10,outputTokens:5}},{type:'finish',reason:{kind:'tool-calls'}}];
export class ScriptAdapter extends LlmAdapter {
 constructor(script=[]){super();this.script=script;this.requests=[];this.active=0;this.maxActive=0;}
 async resolveModel(provider,model){return {provider,id:model,name:model,context:{contextWindow:200000},inputModalities:['text','image']};}
 async *stream(options){
  this.requests.push(options);this.active++;this.maxActive=Math.max(this.maxActive,this.active);
  try{const entry=this.script.shift();const chunks=typeof entry==='function'?await entry(options):entry||textResponse('');for(const chunk of chunks){options.signal?.throwIfAborted();yield chunk;}}
  finally{this.active--;}
 }
}
export async function harness(adapter=new ScriptAdapter(),root) {
 root||=await mkdtemp(join(tmpdir(),'yunying-dsh-native-'));
 const ctx=new Context();
 for(const plugin of [LlmRuntime,SessionStore,SessionProjectionRegistry,SystemPrompt,ToolRuntime,AgentRegistry,Skills])await ctx.plugin(plugin);
 await ctx.plugin(JsonlPersistence,{root:join(root,'sessions'),compression:'none'});
 await ctx.plugin(Attachments,{dshHome:root});
 await ctx.plugin(AgentLoop,{agents:[]});
 await ctx.plugin(TokenMeter);
 await ctx.plugin(Compaction,{auto:false});
 await ctx.plugin(ToolSkill);
 ctx.llm.registerAdapter(['script'],adapter);
 return {ctx,adapter,root};
}
export class FixtureStore {
 constructor(){this.healthy=true;this.maps=new Map();this.rows=new Map();this.bindings=new Map();this.calls=[];this.transcripts=[];this.failDeliveryOnce=false;}
 async mapping(key){if(!this.maps.has(key))this.maps.set(key,{conversation_key:key,dsh_session_id:randomUUID(),provisioning_status:'provisioning',social_state:null});return structuredClone(this.maps.get(key));}
 async mappings(){return [...this.maps.values()].map(v=>structuredClone(v));}
 async policy(key){const row=this.maps.get(key);return {chatMode:row?.chatMode??true};}
 async beginDirect(key,eventId){this.maps.get(key).directEventId=eventId;}
 async endDirect(key,eventId){if(this.maps.get(key).directEventId===eventId)this.maps.get(key).directEventId=null;}
 async ready(key){this.maps.get(key).provisioning_status='ready';}
 async saveState(state){this.maps.get(state.key).social_state=state.snapshot();}
 async accept(payload){if(this.rows.has(payload.eventId))return structuredClone(this.rows.get(payload.eventId));
  const event={event_id:payload.eventId,conversation_key:payload.key,seq:[...this.rows.values()].filter(e=>e.conversation_key===payload.key).length+1,payload,delivered:0,dsh_message:null};this.rows.set(event.event_id,structuredClone(event));return event;}
 async events(key){return [...this.rows.values()].filter(e=>e.conversation_key===key).map(v=>structuredClone(v));}
 async bindMessage(id,message){this.rows.get(id).dsh_message=message;}
 async delivered(id){if(this.failDeliveryOnce){this.failDeliveryOnce=false;throw new Error('simulated SQL acknowledgement failure');}this.rows.get(id).delivered=1;}
 async identity(qq){return {external_id:qq,identity_id:qq};}
 async recordCall(...args){this.calls.push(args);}
 async recordSessionEvents(rows){for(const row of rows)this.transcripts.push(structuredClone(row));}
 async sessionEventFloor(id){return this.transcripts.filter(row=>row.dsh_session_id===id).reduce((floor,row)=>Math.max(floor,row.event_seq),-1);}
}
export const event=(id,key='group:1001',extra={})=>({eventId:id,key,kind:'message',messageId:String(id),userId:'2001',sender:'群友',text:'今天风很舒服',plain:'今天风很舒服',isSelf:false,time:Date.now(),...extra});
export async function durableEvents(ctx,id){const h=await ctx.sessionPersistence.open(id,'read');try{return(await h.read()).events;}finally{await h.close();}}
export async function settle(runtime,key='group:1001'){
 const conv=runtime.conversations.get(key);await conv?.handle.agent.whenIdle();await new Promise(resolve=>setImmediate(resolve));await runtime.ctx.sessions.flush(conv.handle.agent.session);
}
