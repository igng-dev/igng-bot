import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { CuratorRuntime } from '../src/slang-curator.js';
import { harness, ScriptAdapter, toolResponse, textResponse, durableEvents } from './helpers.js';

const job = () => ({ id: randomUUID(), dsh_session_id: randomUUID(), lease_token: randomUUID(), title: '合成字幕',
  config: { provider: 'script', model: 'script', max_steps: 12, max_tokens: 10000, max_seconds: 30, output_tokens: 1000 } });

test('curator uses native DSH, exact scoped tools, evidence submission and separate accounting', async () => {
  const adapter = new ScriptAdapter([toolResponse('read', 'slang_read_material', { after: 0 }), toolResponse('save', 'slang_submit', { candidates: [] })]);
  const { ctx } = await harness(adapter), calls = [], run = job();
  const runtime = new CuratorRuntime(ctx, async (action, data) => { calls.push({ action, data }); return action === 'submit' ? { ok: true, draft_saved: true } : { ok: true, segments: [] }; });
  try {
    ctx.tools.register({ name: 'dangerous_host', description: 'forbidden', parameters: { type: 'object', properties: {} }, output: { schema: { type: 'object' }, render: () => [] }, execute: async () => ({}) });
    await runtime.run(run);
    assert.equal(adapter.requests.length, 2);
    assert.ok(adapter.requests.every(r => r.tools.every(t => ['slang_read_material', 'slang_lookup', 'slang_submit'].includes(t.name))));
    assert.ok(calls.some(c => c.action === 'finish' && c.data.success));
    assert.ok(calls.filter(c => c.action === 'account').every(c => c.data.record.task_type === 'slang_curator'));
    assert.ok((await durableEvents(ctx, run.dsh_session_id)).some(e => e.type === 'tool/call'));
    adapter.script.push(textResponse('不是提交')); await runtime.run(job());
    assert.ok(calls.some(c => c.action === 'finish' && !c.data.success));
  } finally { await runtime.close(); await ctx.fiber.dispose(); }
});

test('curator step budget and accounting outage fail safely; native records can be reconciled', async () => {
  const adapter = new ScriptAdapter([toolResponse('read', 'slang_read_material', { after: 0 })]);
  const { ctx } = await harness(adapter), run = job(), records = [];
  let fail = true, reconciled = false;
  const runtime = new CuratorRuntime(ctx, async (action, data) => {
    if (action === 'account' && fail) throw Error('fixture outbox unavailable');
    if (action === 'recovery') { if (data?.session) { reconciled = true; return { ok: true }; } return { runs: [{ ...run, job_id: run.id }] }; }
    if (action === 'reconcile') records.push(data.record);
    return { ok: true, segments: [] };
  });
  try {
    await assert.rejects(runtime.run(run)); fail = false;
    await runtime.reconcile(); assert.ok(reconciled); assert.ok(records.some(r => r.record_kind === 'attempt'));
    assert.equal(new Set(records.map(r => r.native_event_seq)).size, records.length);
    const budget = job(); budget.config.max_steps = 2;
    adapter.script.push(...Array.from({ length: 5 }, (_, i) => toolResponse('loop' + i, 'slang_read_material', { after: 0 })));
    const before = adapter.requests.length; await runtime.run(budget); assert.ok(adapter.requests.length - before <= 3);
  } finally { await runtime.close(); await ctx.fiber.dispose(); }
});
