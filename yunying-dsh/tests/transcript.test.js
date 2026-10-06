import test from 'node:test';
import assert from 'node:assert/strict';
import { projectEvent, reconstruct, toolResultMode } from '../src/transcript.js';
import { SocialRuntime } from '../src/runtime.js';
import { FixtureStore, harness, ScriptAdapter, testSettings, textResponse, toolResponse, event, settle } from './helpers.js';

test('projectEvent keeps the model-visible flow and drops disposable payloads', () => {
  const assistant = projectEvent('s', { type: 'assistant/message', seq: 3, time: 1000, data: { turn: 1, step: 1,
    message: { source: { kind: 'model', provider: 'p', model: 'm' },
      content: [{ type: 'reasoning', text: '想' }, { type: 'text', text: '答复' },
        { type: 'tool-call', id: 'c1', name: 'qq_send_message', arguments: '{}' }] },
    stream: [{ time: 900, chunk: { type: 'text-delta', delta: '答复' } }], usage: { inputTokens: 1, outputTokens: 2 } } });
  assert.equal(assistant.event_type, 'assistant/message');
  assert.equal(assistant.content, '想\n答复');
  assert.equal(assistant.data.toolCalls[0].name, 'qq_send_message');
  assert.equal(assistant.data.usage.outputTokens, 2);
  assert.ok(!JSON.stringify(assistant).includes('stream'), 'raw provider stream is never projected');

  const call = projectEvent('s', { type: 'tool/call', seq: 4, time: 1001,
    data: { turn: 1, step: 1, callId: 'c1', name: 'qq_send_message', arguments: '{"messages":"hi"}' } });
  assert.equal(call.role, 'tool');
  assert.equal(call.data.name, 'qq_send_message');
  assert.equal(call.content, '{"messages":"hi"}');

  const result = projectEvent('s', { type: 'tool/result', seq: 5, time: 1002,
    data: { turn: 1, step: 1, message: { toolCallId: 'c1', isError: false, content: [{ type: 'text', text: 'x'.repeat(5000) }] } } });
  assert.equal(result.content, null, 'tool result body is dropped');
  assert.equal(result.data.stored, 'dropped');
  assert.equal(result.data.isError, false);
  assert.equal(result.data.bytes, 5000);

  assert.equal(projectEvent('s', { type: 'agent/inbox/spliced', seq: 6, time: 1003,
    data: { target: 'next-turn', start: 0, inserted: [] } }), null, 'inbox splices duplicate user messages');

  const header = projectEvent('s', { type: 'request/header', seq: 7, time: 1004,
    data: { reason: 'initial', header: { config: { provider: 'p', model: 'm', maxTokens: 4096 }, tools: [{ name: 'big' }] } } });
  assert.deepEqual(header.data, { reason: 'initial', provider: 'p', model: 'm', maxTokens: 4096 });

  const end = projectEvent('s', { type: 'turn/end', seq: 8, time: 1005, data: { turn: 1, reason: { kind: 'completed' } } });
  assert.equal(end.event_type, 'turn/end');
  assert.equal(end.data.reason, 'completed');
});

test('toolResultMode defaults to none and rejects unknown values', () => {
  assert.equal(toolResultMode({}), 'none');
  assert.equal(toolResultMode({ YUNYING_SESSION_TOOL_RESULTS: 'preview' }), 'preview');
  assert.equal(toolResultMode({ YUNYING_SESSION_TOOL_RESULTS: 'bogus' }), 'none');
});

test('live turns project into MySQL rows that reconstruct in order', async () => {
  const adapter = new ScriptAdapter(), { ctx } = await harness(adapter), store = new FixtureStore();
  const runtime = new SocialRuntime(ctx, store, testSettings(), async () => ({ ok: true }));
  try {
    await runtime.load('group:1001');
    const conv = runtime.conversations.get('group:1001');
    const args = () => ({ key: conv.state.key, token: conv.state.agentToken });
    adapter.script.push(() => toolResponse('observe', 'qq_get_unread_messages', { ...args() }), textResponse(''));
    await runtime.accept(event('project-1', 'group:1001', { atBot: true, text: '你好' }));
    await settle(runtime);
    await runtime.flushTranscripts();
    const rows = store.transcripts.filter(row => row.dsh_session_id === conv.state.sessionId);
    const types = new Set(rows.map(row => row.event_type));
    for (const type of ['turn/start', 'user/message', 'assistant/message', 'tool/call', 'tool/result', 'turn/end'])
      assert.ok(types.has(type), `missing ${type}`);
    assert.equal(rows.find(row => row.event_type === 'tool/result').content, null);
    const ordered = reconstruct(rows);
    assert.deepEqual(ordered.map(item => item.seq), [...ordered.map(item => item.seq)].sort((left, right) => left - right));
    assert.ok(ordered.some(item => item.role === 'user' && item.text.includes('你好')));
  } finally { await runtime.close(); await ctx.fiber.dispose(); }
});

test('restart backfills the projection from the official log without duplicates', async () => {
  const store = new FixtureStore();
  const first = await harness();
  let runtime = new SocialRuntime(first.ctx, store, testSettings(), async () => ({ ok: true }));
  await runtime.load('group:1001');
  const sessionId = runtime.conversations.get('group:1001').state.sessionId;
  await runtime.accept(event('backfill-1', 'group:1001', { atBot: true, text: '记住这句话' }));
  await settle(runtime);
  await runtime.flushTranscripts();
  const before = store.transcripts.filter(row => row.dsh_session_id === sessionId);
  assert.ok(before.length > 0);
  await runtime.close(); await first.ctx.fiber.dispose();

  store.transcripts = [];  // simulate a lost projection; the authoritative official log still holds the events
  const second = await harness(new ScriptAdapter(), first.root);
  runtime = new SocialRuntime(second.ctx, store, testSettings(), async () => ({ ok: true }));
  try {
    await runtime.restore();
    await runtime.flushTranscripts();
    const after = store.transcripts.filter(row => row.dsh_session_id === sessionId);
    assert.ok(after.length >= before.length);
    for (const row of before)
      assert.ok(after.some(candidate => candidate.event_seq === row.event_seq), `backfilled seq ${row.event_seq}`);
    assert.equal(new Set(after.map(row => row.event_seq)).size, after.length, 'one row per event seq');
  } finally { await runtime.close(); await second.ctx.fiber.dispose(); }
});
