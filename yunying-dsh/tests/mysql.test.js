import test from 'node:test';import assert from 'node:assert/strict';import {randomUUID}from'node:crypto';
import {MySQLStore}from'../src/store.js';
const enabled=process.env.YUNYING_TEST_DB==='yunying_v4_test';
const open=()=>MySQLStore.open({DB_HOST:'127.0.0.1',DB_PORT:process.env.YUNYING_TEST_DB_PORT||'33316',DB_USER:'root',DB_NAME:'yunying_v4_test'});
async function source(store,key,qq,text){await store.mapping(key);const event=await store.accept({eventId:randomUUID(),key,kind:'message',messageId:String(Math.floor(Math.random()*1e10)),userId:qq,text,plain:text,isSelf:false});return event;}
const actor=(key,events)=>({key,sessionId:randomUUID(),seenSeqs:new Set(events.map(e=>e.seq))});
test('real MySQL: Person sharing consent, cross-group update, CAS, forget, revisions/rollback and private-scope non-disclosure',{skip:!enabled},async()=>{
 const store=await open();const suffix=randomUUID();
 try{
  const qq=String(BigInt('8000000000')+BigInt(Math.floor(Math.random()*1e9)));const binding=await store.identity(qq,'fixture');
  const a=await source(store,'group:1001',qq,`我喜欢${suffix}手冲咖啡`),b=await source(store,'group:1002',qq,`现在改喝${suffix}红茶了`);
  const aa=actor('group:1001',[a]),bb=actor('group:1002',[b]);
  const privateDoc=(await store.memoryWrite(aa,{title:'群内私有 '+suffix,markdown:'不可跨群的事实 '+suffix,sources:[a.event_id],reason:'fixture'})).document;
  await assert.rejects(store.memoryRead(bb,privateDoc.id));const hidden=await store.memorySearch(bb,{query:suffix});assert.equal(hidden.documents.length,0);
  const args={title:'will be generated',markdown:'untrusted proposed shared text',visibility:'shared_person',personQQ:qq,sources:[a.event_id],reason:'本人稳定偏好'};
  await assert.rejects(store.memoryWrite(aa,args));
  await store.query("UPDATE memory_identity_bindings SET shared_memory_opt_in=1 WHERE provider='qq' AND external_id=?",[qq]);
  const shared=(await store.memoryWrite(aa,args)).document;assert.equal(shared.markdown,`> 我喜欢${suffix}手冲咖啡`);assert.ok(!shared.markdown.includes('untrusted'));
  assert.equal((await store.memoryRead(bb,shared.id)).document.current_version,1);
  const updated=(await store.memoryUpdate(bb,{id:shared.id,expectedVersion:1,markdown:'cannot launder other group data',sources:[b.event_id],reason:'本人新偏好覆盖旧偏好'})).document;
  assert.equal(updated.current_version,2);assert.equal(updated.markdown,`> 现在改喝${suffix}红茶了`);
  await assert.rejects(store.memoryUpdate(bb,{id:shared.id,expectedVersion:1,markdown:'stale overwrite',sources:[b.event_id],reason:'fixture'}));
  await store.query("UPDATE memory_identity_bindings SET shared_memory_opt_in=0 WHERE provider='qq' AND external_id=?",[qq]);await assert.rejects(store.memoryRead(bb,shared.id));
  await store.query("UPDATE memory_identity_bindings SET shared_memory_opt_in=1 WHERE provider='qq' AND external_id=?",[qq]);
  await assert.rejects(store.memoryWrite(actor('private:2001',[a]),args));
  const stranger=await source(store,'group:1002','2999','第三人的意见');await assert.rejects(store.memoryUpdate(actor('group:1002',[stranger]),{id:shared.id,expectedVersion:2,markdown:'private copy',sources:[stranger.event_id],reason:'fixture'}));
  const forget=await source(store,'group:1002',qq,'请忘记我的茶偏好');
  await store.memoryUpdate(actor('group:1002',[forget]),{id:shared.id,expectedVersion:2,sources:[forget.event_id],reason:'本人要求遗忘'},true);await assert.rejects(store.memoryRead(bb,shared.id));
  const versions=await store.adminVersions(shared.id);assert.equal(versions.length,3);assert.equal(versions[0].status,'forgotten');
  await store.adminRollback(shared.id,1,3,'owner controlled rollback');assert.equal((await store.memoryRead(bb,shared.id)).document.current_version,4);
  assert.ok((await store.query('SELECT id FROM memory_audit WHERE document_id=?',[shared.id])).length>=6);
  assert.equal(binding.identity_id,(await store.identity(qq)).identity_id);
 }finally{await store.close();}
});
test('real MySQL: event deduplication and one runtime lease block competing native Agents',{skip:!enabled},async()=>{
 const store=await open();try{
  await assert.rejects(open(),/another YunYing/);
  await store.mapping('group:1001');const payload={eventId:randomUUID(),key:'group:1001',kind:'message',messageId:'100',userId:'2001',text:'lease fixture'};
  const first=await store.accept(payload),second=await store.accept(payload);assert.equal(first.seq,second.seq);await store.heartbeat();assert.equal(store.healthy,true);
 }finally{await store.close();}
});
