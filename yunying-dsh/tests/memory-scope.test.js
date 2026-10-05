import test from 'node:test';import assert from 'node:assert/strict';
import {MySQLStore, dbSslOptions} from '../src/store.js';
import {SocialState} from '../src/social.js';

test('mysql TLS options follow DB_SSL and only verify when asked',()=>{
  assert.equal(dbSslOptions({}),null);
  assert.equal(dbSslOptions({DB_SSL:'0'}),null);
  assert.deepEqual(dbSslOptions({DB_SSL:'1'}),{rejectUnauthorized:false});
  assert.deepEqual(dbSslOptions({DB_SSL:'true',DB_SSL_VERIFY:'1'}),{rejectUnauthorized:true});
});

test('person memory visibility aggregates the IGNG account; group memory stays scoped',async()=>{
  const store=new MySQLStore({},{}); // access() only builds SQL, it never touches the pool
  store.ownerResolver=async qqs=>{const set=new Set(qqs.map(String));if(set.has('2001')||set.has('2002')){set.add('2001');set.add('2002');}return [...set];};
  const groupOnly=await store.access({key:'group:1001',qqs:[]});
  assert.match(groupOnly.sql,/document_type='group' AND d\.scope_key=\?/);
  assert.deepEqual(groupOnly.values,['group:1001']);
  const account=await store.access({key:'group:1001',qqs:['2001']});
  assert.equal(account.values[0],'group:1001');
  assert.deepEqual(new Set(account.values.slice(1)),new Set(['2001','2002']));
  const single=await store.access({key:'private:2999',qqs:['2999']});
  assert.deepEqual(single.values,['private:2999','2999']);
  assert.deepEqual(await store.access({admin:true,key:'owner'}),{sql:'1=1',values:[]});
});

test('a resolver failure never widens access to other accounts',async()=>{
  const store=new MySQLStore({},{});
  store.ownerResolver=async()=>{throw new Error('infrastructure unavailable');};
  const filter=await store.access({key:'group:1001',qqs:['2001']});
  assert.deepEqual(filter.values,['group:1001','2001']);
});

test('recent speakers provide the QQ set for group memory reads',()=>{
  const state=new SocialState('group:1001','sess',{});
  state.recentMessages=[
    {seq:1,userId:'2002',isSelf:false,text:'a'},
    {seq:2,userId:'2001',isSelf:false,text:'b'},
    {seq:3,userId:'2001',isSelf:false,text:'c'},
    {seq:4,userId:'3001',isSelf:true,text:'d'},
  ];
  assert.deepEqual(state.actorQqs(),['2001','2002']);
  const priv=new SocialState('private:2005','sess',{});
  assert.deepEqual(priv.actorQqs(),['2005']);
});
