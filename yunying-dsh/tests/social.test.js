import test from 'node:test';import assert from 'node:assert/strict';import { SocialState } from '../src/social.js';
const state=()=>new SocialState('group:1001','test',{}, {preSleepWaitMs:300000,minQuietMs:10000,nicknames:['云萤']});
const append=(s,seq,text='普通消息')=>s.append({seq,payload:{messageId:String(seq),text,plain:text,userId:'2001',isSelf:false}});
test('reserved2 watermark: tail pages cannot acknowledge unseen gaps or arrivals',()=>{
 const s=state();for(let seq=1;seq<=120;seq++)append(s,seq);
 assert.equal(s.unreadPage(30).readThroughSeq,0);assert.throws(()=>s.acknowledge(120));
 assert.equal(s.unreadPage(100,0).readThroughSeq,120); // previously seen tail now closes the continuous gap
 append(s,121);s.acknowledge(120);assert.equal(s.unread.length,1);assert.equal(s.unread[0].seq,121);
});
test('reserved2 bounded wake snapshot is oldest first, with partial=true and no unseen receipt',()=>{
 const s=state();for(let seq=1;seq<=30;seq++)append(s,seq,'消息内容'.repeat(80));
 const packet=s.wakeSnapshot();assert.ok(packet.partial);assert.equal(packet.messages[0].seq,1);assert.ok(JSON.stringify(packet).length<=6000);assert.equal(s.readThrough(),0);
 s.note(packet.messages);assert.equal(s.readThrough(),packet.readThroughSeq);assert.throws(()=>s.acknowledge(30));
});
test('reserved2 reply wait includes thinking-time arrivals and cannot grant sleep credit',async()=>{
 let now=100000;const s=new SocialState('group:1001','test',{}, {preSleepWaitMs:300000,minQuietMs:10000},()=>now);
 append(s,1);s.note(s.unread);now+=20000;append(s,2);now+=10000;
 const result=await s.wait({purpose:'reply',timeoutMs:5000},new AbortController().signal,async ms=>{now+=ms;});
 assert.equal(result.quiet,true);assert.equal(result.waitedMs,0);assert.equal(result.newMessages[0].seq,2);assert.equal(result.preSleepWaitSatisfied,false);assert.equal(s.preSleepBlocked(),true);
});
test('reserved2 messages wait: no arrivals waits full timeout; short calls do not accumulate sleep credit',async()=>{
 let now=100000;const s=new SocialState('group:1001','test',{}, {preSleepWaitMs:300000,minQuietMs:10000},()=>now);append(s,1);
 for(let n=0;n<2;n++){const result=await s.wait({timeoutMs:5000},new AbortController().signal,async ms=>{now+=ms;});assert.equal(result.waitedMs,5000);assert.equal(result.arrived,false);assert.equal(result.preSleepWaitSatisfied,false);}
 assert.ok(s.preSleepBlocked());
});
test('reserved2 long observation sees new messages, read-and-silence can close; sending invalidates credit',async()=>{
 let now=100000,arrived=false;const s=new SocialState('group:1001','test',{}, {preSleepWaitMs:300000,minQuietMs:10000},()=>now);append(s,1);
 const result=await s.wait({timeoutMs:300000},new AbortController().signal,async ms=>{now+=ms;if(!arrived&&now>=101000){append(s,2);arrived=true;}});
 assert.equal(result.preSleepWaitObserved,true);assert.equal(result.preSleepWaitSatisfied,false);assert.equal(result.quiet,true);assert.equal(s.preSleepBlocked(),false);
 s.sent('900','回来一句');assert.equal(s.preSleepBlocked(),true);
});
test('reserved2 reply timeout is not evidence the speaker is done, cancellation is cooperative',async()=>{
 let now=100000;const s=new SocialState('group:1001','test',{}, {preSleepWaitMs:300000,minQuietMs:10000},()=>now);append(s,1);
 const result=await s.wait({purpose:'reply',timeoutMs:5000},new AbortController().signal,async ms=>{now+=ms;});assert.equal(result.quiet,false);assert.equal(result.timeout,true);
 const abort=new AbortController();await assert.rejects(s.wait({timeoutMs:300000},abort.signal,async()=>{abort.abort(new Error('cancel'));}));assert.equal(s.waiting,false);
});
test('reserved2 direct mentions/private wake, optional speaker IDs, finite and infinite diving safety',()=>{
 const s=state();assert.equal(s.wakeReason({atBot:true}),'atMention');assert.equal(s.wakeReason({replyToBot:true}),'atMention');assert.equal(s.wakeReason({text:'无关闲聊'},()=>1),null);
 s.wakeConfig.triggers.speakerIds=['2001'];assert.equal(s.wakeReason({userId:'2001',text:'无关闲聊'},()=>1),'speaker');
 append(s,1,'晚安');s.note(s.unread);s.setWake({infinite:false,sleepMs:60000},1);assert.ok(Date.parse(s.wakeConfig.sleepUntil)>Date.now());
 s.setWake({infinite:true,triggers:{atMention:false,nameMention:false,question:false,poke:false,probability:0,speakerIds:[],keywords:[]}});assert.equal(s.wakeConfig.triggers.atMention,true);
});
test('default wake probability is 0.20 and the old 0.05 default migrates once',()=>{
 const fresh=state();assert.equal(fresh.wakeConfig.triggers.probability,0.20);
 const legacy=new SocialState('group:1001','test',{wakeConfig:{...fresh.wakeConfig,triggers:{...fresh.wakeConfig.triggers,probability:0.05}}});
 assert.equal(legacy.wakeConfig.triggers.probability,0.20);
 const custom=new SocialState('group:1001','test',{wakeConfig:{...fresh.wakeConfig,triggers:{...fresh.wakeConfig.triggers,probability:0.3}}});
 assert.equal(custom.wakeConfig.triggers.probability,0.3);
});
test('recall tombstone removes content and media from unread/recent views',()=>{
 const s=state();append(s,1,'秘密内容');s.unread[0].media=[{type:'image'}];s.append({seq:2,payload:{kind:'recall',messageId:'1',text:'[消息已撤回]'}});
 const row=s.unreadPage(10,0).messages[0];assert.equal(row.text,'[消息已撤回]');assert.equal(row.plain,undefined);assert.deepEqual(row.media,undefined);
});
