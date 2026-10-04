import test from 'node:test';import assert from 'node:assert/strict';import {randomUUID}from'node:crypto';
import {MySQLStore}from'../src/store.js';import {SocialRuntime}from'../src/runtime.js';
import {harness,ScriptAdapter,testSettings,textResponse,event,settle,durableEvents}from'./helpers.js';
const enabled=process.env.YUNYING_TEST_DB==='yunying_v4_test';
const open=()=>MySQLStore.open({DB_HOST:'127.0.0.1',DB_PORT:process.env.YUNYING_TEST_DB_PORT||'33316',DB_USER:'root',DB_NAME:'yunying_v4_test'});
test('real MySQL + native DSH: official Session mapping, Person identity and Memory survive restart',{skip:!enabled},async()=>{
 const key='group:'+String(900000000+Math.floor(Math.random()*1e8)),qq=String(700000000+Math.floor(Math.random()*1e8));
 const config=testSettings();config.groups.clear();config.private.clear();config.groups.add(key.split(':')[1]);
 let store=await open(),h=await harness(),runtime=new SocialRuntime(h.ctx,store,config,async()=>({ok:true}));let id,doc,root=h.root;
 try{
  const source=event(randomUUID(),key,{userId:qq,text:'我喜欢红茶',plain:'我喜欢红茶',atBot:true});await runtime.accept(source);await settle(runtime,key);
  const conv=runtime.conversations.get(key);id=conv.state.sessionId;
  // Use the real DSH tool scheduler for Memory instead of bypassing its permission boundary.
  const args={key,token:conv.state.agentToken,title:'偏好',markdown:'# 偏好\n喜欢红茶',sources:[source.eventId],reason:'本人稳定偏好',personQQ:qq};
  const result=await conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'remember-real',name:'memory_write',arguments:args,signal:new AbortController().signal});
  assert.equal(result.isError,false);doc=JSON.parse(result.content[0].text).document;
  await runtime.close();await h.ctx.fiber.dispose();await store.close();
  store=await open();h=await harness(new ScriptAdapter([textResponse('')]),root);runtime=new SocialRuntime(h.ctx,store,config,async()=>({ok:true}));await runtime.restore();
  const after=runtime.conversations.get(key);assert.equal(after.state.sessionId,id);
  const read=await after.handle.agent.ctx.tools.execute({agent:after.handle.agent,callId:'recall-real',name:'memory_read',arguments:{key,token:after.state.agentToken,id:doc.id},signal:new AbortController().signal});
  assert.equal(read.isError,false);assert.ok(JSON.parse(read.content[0].text).document.markdown.includes('红茶'));
  await runtime.accept(event(randomUUID(),key,{userId:qq,replyToBot:true}));await settle(runtime,key);
  const history=await durableEvents(h.ctx,id);assert.ok(history.some(e=>e.type==='user/message'));
  const calls=await store.query('SELECT record_id FROM yunying_ai_records WHERE dsh_session_id=?',[id]);assert.ok(calls.length>=2);
 }finally{await runtime.close();await h.ctx.fiber.dispose();await store.close();}
});
