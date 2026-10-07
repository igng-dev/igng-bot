import test from 'node:test';
import assert from 'node:assert/strict';
import { SocialRuntime } from '../src/runtime.js';
import { FixtureStore,harness,testSettings } from './helpers.js';

test('terminal video tools route opaque handles through infrastructure only',async()=>{
 const calls=[],{ctx}=await harness();
 const runtime=new SocialRuntime(ctx,new FixtureStore(),testSettings(),async(path,body)=>{
  calls.push({path,body});
  if(path==='/terminal/open')return {ok:true,sessionId:'s'.repeat(24),inputId:'11111111-1111-1111-1111-111111111111',
   name:'clip.mp4',mediaType:'video/mp4',size:10,expiresInSec:1800};
  if(path==='/terminal/send')return {ok:true,status:'sent',message_id:'9001'};
  return {ok:true,kind:'probe',metadata:{duration:'1.0'}};
 });
 try{
  await runtime.load('group:1001');const conv=runtime.conversations.get('group:1001');
  const execute=(name,args)=>conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'fixture-'+name,
   name,arguments:args,signal:new AbortController().signal});
  const base={key:conv.state.key,token:conv.state.agentToken};
  const prompt=JSON.parse((await execute('qq_get_prompt',{...base})).content[0].text);
  assert.ok(prompt.enabledTools.includes('qq_video_open'));
  assert.ok(prompt.enabledTools.includes('qq_send_artifact'));

  const opened=await execute('qq_video_open',{...base,messageId:'501'});
  assert.equal(opened.isError,false);
  const handles=JSON.parse(opened.content[0].text);
  assert.equal(handles.sessionId,'s'.repeat(24));
  assert.equal(handles.inputId,'11111111-1111-1111-1111-111111111111');
  assert.equal(calls.at(-1).path,'/terminal/open');
  assert.deepEqual(calls.at(-1).body,{key:'group:1001',token:conv.state.agentToken,messageId:'501',attachmentIndex:0});
  assert.ok(!JSON.stringify(calls.at(-1).body).includes('/'));

  const probed=await execute('qq_video_probe',{...base,sessionId:handles.sessionId,inputId:handles.inputId});
  assert.equal(probed.isError,false);
  assert.equal(calls.at(-1).path,'/terminal/probe');
  assert.equal(calls.at(-1).body.sessionId,handles.sessionId);
  assert.ok(!JSON.stringify(probed).includes('/terminal/'));

  const sent=await execute('qq_send_artifact',{...base,sessionId:handles.sessionId,artifactId:'a'.repeat(36)});
  assert.equal(sent.isError,false);
  assert.equal(calls.at(-1).path,'/terminal/send');
  assert.equal(calls.at(-1).body.requestId,`${conv.state.sessionId}:fixture-qq_send_artifact:artifact`);
  assert.equal(conv.state.sendTimes.length,1);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});

test('terminal tools reject foreign sessions and unbounded parameters before infrastructure',async()=>{
 const calls=[],{ctx}=await harness();
 const runtime=new SocialRuntime(ctx,new FixtureStore(),testSettings(),async(path,body)=>{calls.push({path,body});return {ok:true};});
 try{
  await runtime.load('group:1001');const conv=runtime.conversations.get('group:1001');
  const execute=(name,args)=>conv.handle.agent.ctx.tools.execute({agent:conv.handle.agent,callId:'fixture-'+name,
   name,arguments:args,signal:new AbortController().signal});
  const base={key:conv.state.key,token:conv.state.agentToken};
  const wrongToken=await execute('qq_video_probe',{...base,token:'f'.repeat(32),
   sessionId:'s'.repeat(24),inputId:'11111111-1111-1111-1111-111111111111'});
  assert.equal(wrongToken.isError,true);
  const badIndex=await execute('qq_video_open',{...base,messageId:'501',attachmentIndex:99});
  assert.equal(badIndex.isError,true);
  assert.equal(calls.length,0);
 }finally{await runtime.close();await ctx.fiber.dispose();}
});
