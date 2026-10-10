import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { MySQLStore } from '../src/store.js';
import { SocialRuntime } from '../src/runtime.js';
import { canonicalKnowledgeKey, knowledgeTerms, quoteFromSearch, renderKnowledge, shouldConsultKnowledge } from '../src/knowledge.js';
import { PolicyError } from '../src/policy.js';
import { FixtureStore, event, harness, settle, testSettings } from './helpers.js';

test('knowledge key, terms and quote gate reject model-only claims', () => {
  assert.equal(canonicalKnowledgeKey(' Paper:1.21.1:gamemode '), 'paper:1.21.1:gamemode');
  assert.throws(() => canonicalKnowledgeKey('闲聊'), PolicyError);
  assert.equal(shouldConsultKnowledge('哈哈'), false);
  assert.equal(shouldConsultKnowledge('Paper 1.21 的 gamemode 怎么用'), true);
  assert.ok(knowledgeTerms('Paper服务器').includes('paper'));
  const hit = { title: 'Paper docs', url: 'https://docs.papermc.io/paper', snippet: 'The gamemode command changes a player mode.' };
  const quoted = quoteFromSearch(hit, 'The gamemode command changes a player mode. 适用于 Paper。');
  assert.equal(quoted.excerpt, hit.snippet);
  assert.throws(() => quoteFromSearch(hit, '我推测这个命令还能改世界规则'), PolicyError);
  assert.match(renderKnowledge([{ status: 'stale', category: 'software', title: '旧事实', claim: '摘录', source_url: 'https://example.org', valid_until: 'x', fresh: false }]), /已过期/);
  assert.equal(renderKnowledge([]), '');
});

test('knowledge is injected only when the wake text can match, and failure stays silent', async () => {
  const { ctx } = await harness();
  const store = new FixtureStore();
  store.knowledgeSearch = async ({ query }) => {
    assert.match(query, /Paper/);
    return { documents: [{ id: 'k1', status: 'active', category: 'software', title: 'Paper gamemode', claim: 'The gamemode command changes a player mode.', source_url: 'https://docs.papermc.io/paper', valid_until: '2099-01-01 00:00:00.000000', fresh: true }] };
  };
  const runtime = new SocialRuntime(ctx, store, testSettings(), async () => ({ ok: true }));
  await runtime.accept(event('paper-question', 'group:1001', { text: 'Paper 的 gamemode 怎么用', plain: 'Paper 的 gamemode 怎么用' }));
  await settle(runtime);
  const conv = runtime.conversations.get('group:1001');
  const reader = await ctx.sessionPersistence.open(conv.handle.agent.session.id, 'read');
  const events = (await reader.read()).events;
  await reader.close();
  const wake = events.filter(item => item.type === 'user/message').map(item => item.data.content?.[0]?.text || '').join('\n');
  assert.match(wake, /外置知识库/);
  assert.match(wake, /gamemode command/);
  store.knowledgeSearch = async () => { throw new Error('knowledge backend unavailable'); };
  await runtime.accept(event('paper-again', 'group:1001', { text: '再问一次 Paper gamemode', plain: '再问一次 Paper gamemode' }));
  await settle(runtime);
  assert.equal(conv.handle.agent.status, 'idle');
  await runtime.close();
  await ctx.fiber.dispose();
});

const enabled = process.env.YUNYING_TEST_DB === 'yunying_v4_test';
test('real MySQL: proposal quotes the search, conflict demotes, and owner promotes', { skip: !enabled }, async () => {
  const store = await MySQLStore.open({ DB_HOST: '127.0.0.1', DB_PORT: process.env.YUNYING_TEST_DB_PORT || '33316', DB_USER: 'root', DB_NAME: 'yunying_v4_test' });
  const suffix = randomUUID().slice(0, 8);
  try {
    const key = `paper:${suffix}:gamemode`;
    const actor = { key: 'group:1001', sessionId: randomUUID() };
    store.noteKnowledgeSearch(actor.key, { results: [{ title: 'Paper', url: 'https://docs.papermc.io/paper', snippet: `mode ${suffix} is survival creative adventure spectator` }] });
    const searchId = [...store.knowledgeSearches.get(actor.key)].at(-1).id;
    await assert.rejects(store.knowledgePropose(actor, { key, title: '猜测', claim: '模型自己的结论', category: 'software', searchId, reason: 'fixture' }), /摘录/);
    const proposed = await store.knowledgePropose(actor, { key, title: 'Paper gamemode', claim: `mode ${suffix} is survival creative adventure spectator`, category: 'software', searchId, reason: '以后还会问' });
    assert.equal(proposed.document.status, 'provisional');
    const again = await store.knowledgePropose(actor, { key, title: 'Paper gamemode', claim: `mode ${suffix} is survival creative adventure spectator`, category: 'software', searchId, reason: '重复' });
    assert.equal(again.unchanged, true);
    store.noteKnowledgeSearch(actor.key, { results: [{ title: 'Paper', url: 'https://docs.papermc.io/paper/new', snippet: `mode ${suffix} changed in this build` }] });
    const nextId = [...store.knowledgeSearches.get(actor.key)].at(-1).id;
    await store.knowledgeAdmin({ id: proposed.document.id, operation: 'promote', expectedVersion: proposed.document.currentVersion, reason: 'owner verified' });
    const conflict = await store.knowledgePropose(actor, { key, title: 'Paper gamemode', claim: `mode ${suffix} changed in this build`, category: 'software', searchId: nextId, reason: '来源变了' });
    assert.equal(conflict.document.status, 'stale');
    const found = await store.knowledgeSearch({ query: suffix });
    assert.equal(found.documents[0].fresh, false);
    await store.knowledgeAdmin({ id: proposed.document.id, operation: 'retract', expectedVersion: conflict.document.currentVersion, reason: 'owner retracted' });
    assert.equal((await store.knowledgeSearch({ query: suffix })).documents.length, 0);
  } finally { await store.close(); }
});

test('prompt advertises knowledge tools without treating them as memory', async () => {
  const { ctx } = await harness();
  const store = new FixtureStore();
  store.knowledgeSearch = async () => ({ ok: true, documents: [] });
  const runtime = new SocialRuntime(ctx, store, testSettings(), async () => ({ ok: true }));
  await runtime.accept(event('quiet', 'group:1001', { text: '在吗', plain: '在吗' }));
  await settle(runtime);
  const conv = runtime.conversations.get('group:1001');
  const raw = await conv.handle.agent.ctx.tools.execute({ agent: conv.handle.agent, callId: 'prompt', name: 'qq_get_prompt', arguments: { key: 'group:1001', token: conv.state.agentToken }, signal: new AbortController().signal });
  const prompt = JSON.parse(raw.content[0].text);
  assert.ok(prompt.enabledTools.includes('knowledge_propose'));
  assert.ok(prompt.enabledTools.includes('knowledge_search'));
  assert.ok(prompt.enabledTools.includes('knowledge_use'));
  assert.equal(prompt.worldKnowledge.autoInject, true);
  await runtime.close();
  await ctx.fiber.dispose();
});
