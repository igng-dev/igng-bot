import test from 'node:test';import assert from 'node:assert/strict';import {randomUUID}from'node:crypto';
import {MySQLStore}from'../src/store.js';
const enabled=process.env.YUNYING_TEST_DB==='yunying_v4_test';
const open=()=>MySQLStore.open({DB_HOST:'127.0.0.1',DB_PORT:process.env.YUNYING_TEST_DB_PORT||'33316',DB_USER:'root',DB_NAME:'yunying_v4_test'});
async function source(store,key,qq,text){await store.mapping(key);const event=await store.accept({eventId:randomUUID(),key,kind:'message',messageId:String(Math.floor(Math.random()*1e10)),userId:qq,text,plain:text,isSelf:false});return event;}
const actor=(key,events,qqs=[])=>({key,sessionId:randomUUID(),seenSeqs:new Set(events.map(e=>e.seq)),qqs});
test('real MySQL: QQ-keyed cross-group person memory, group scope, IGNG-account aggregation, CAS, forget and rollback',{skip:!enabled},async()=>{
 const store=await open();const suffix=randomUUID();
 try{
  const qqA=String(BigInt('8000000000')+BigInt(Math.floor(Math.random()*1e8)));
  const qqB=String(BigInt('8000000000')+BigInt(100000000+Math.floor(Math.random()*1e8)));
  // Both QQs belong to the same IGNG account; the resolver aggregates them.
  store.ownerResolver=async qqs=>{const set=new Set(qqs.map(String));if(set.has(qqA)||set.has(qqB)){set.add(qqA);set.add(qqB);}return [...set];};
  const a=await source(store,'group:1001',qqA,`我喜欢${suffix}手冲咖啡`),b=await source(store,'group:1002',qqA,`现在改喝${suffix}红茶了`),c=await source(store,'group:1002',qqB,`我替朋友记一下${suffix}`);
  const aa=actor('group:1001',[a],[qqA]),bb=actor('group:1002',[b],[qqA]),cc=actor('group:1002',[c],[qqB]);
  // Group memory stays scoped to its conversation.
  const groupDoc=(await store.memoryWrite(aa,{title:'群内 '+suffix,markdown:'不可跨群的事实 '+suffix,sources:[a.event_id],reason:'fixture'})).document;
  assert.equal(groupDoc.document_type,'group');
  await assert.rejects(store.memoryRead(bb,groupDoc.id));
  assert.equal((await store.memorySearch(bb,{query:suffix})).documents.length,0);
  // Person memory is cross-group; any QQ of the same account may read/update it.
  const args={title:'will be generated',markdown:'untrusted proposed text',personQQ:qqA,sources:[a.event_id],reason:'本人稳定偏好'};
  const shared=(await store.memoryWrite(aa,args)).document;
  assert.equal(shared.document_type,'person');assert.equal(shared.person_qq,qqA);
  assert.equal(shared.markdown,`> 我喜欢${suffix}手冲咖啡`);assert.ok(!shared.markdown.includes('untrusted'));
  assert.equal((await store.memoryRead(cc,shared.id)).document.current_version,1);
  const updated=(await store.memoryUpdate(bb,{id:shared.id,expectedVersion:1,markdown:'cannot launder other group data',sources:[b.event_id],reason:'本人新偏好覆盖旧偏好'})).document;
  assert.equal(updated.current_version,2);assert.equal(updated.markdown,`> 现在改喝${suffix}红茶了`);
  await assert.rejects(store.memoryUpdate(bb,{id:shared.id,expectedVersion:1,markdown:'stale overwrite',sources:[b.event_id],reason:'fixture'}));
  const stranger=await source(store,'group:1002','2999','第三人的意见');
  await assert.rejects(store.memoryUpdate(actor('group:1002',[stranger],['2999']),{id:shared.id,expectedVersion:2,markdown:'private copy',sources:[stranger.event_id],reason:'fixture'}));
  const forget=await source(store,'group:1002',qqA,'请忘记我的茶偏好');
  await store.memoryUpdate(actor('group:1002',[forget],[qqA]),{id:shared.id,expectedVersion:2,sources:[forget.event_id],reason:'本人要求遗忘'},true);
  await assert.rejects(store.memoryRead(bb,shared.id));
  const versions=await store.adminVersions(shared.id);assert.equal(versions.length,3);assert.equal(versions[0].status,'forgotten');
  assert.ok(versions[0].sources.some(s=>s.event_id===forget.event_id));
  assert.ok(versions[1].sources.some(s=>s.event_id===b.event_id));
  assert.ok(versions[2].sources.some(s=>s.event_id===a.event_id));
  await store.adminRollback(shared.id,1,3,'owner controlled rollback');assert.equal((await store.memoryRead(bb,shared.id)).document.current_version,4);
  assert.equal(typeof store.identity,'undefined');
 }finally{await store.close();}
});
test('real MySQL: event deduplication and one runtime lease block competing native Agents',{skip:!enabled},async()=>{
 const store=await open();try{
  await assert.rejects(open(),/another YunYing/);
  await store.mapping('group:1001');const payload={eventId:randomUUID(),key:'group:1001',kind:'message',messageId:'100',userId:'2001',text:'lease fixture'};
  const first=await store.accept(payload),second=await store.accept(payload);assert.equal(first.seq,second.seq);await store.heartbeat();assert.equal(store.healthy,true);
 }finally{await store.close();}
});
