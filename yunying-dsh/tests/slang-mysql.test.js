import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { MySQLStore } from '../src/store.js';
import { SlangContext } from '../src/slang.js';

test('real SQL slang snapshot generation, publication filtering and scoped opt-in observation deduplication', { skip: process.env.YUNYING_TEST_DB !== 'yunying_v4_test' }, async () => {
  const store = await MySQLStore.open({ DB_HOST: '127.0.0.1', DB_PORT: process.env.YUNYING_TEST_DB_PORT || '33316', DB_USER: 'root', DB_NAME: 'yunying_v4_test' });
  const id = 'fixture:' + randomUUID(), scope = 'group:' + String(7000000000 + Math.floor(Math.random() * 100000));
  const payload = { id, name: '专用合成梗', meaning: '当前 fixture 的合成解释', tone: '调侃', aliases: [{ text: '专用合成梗' }], contexts: ['casual'], topics: [], risks: [], confidence: 'high', lifecycle: 'unknown', usage: 'understand_only', sources: [{ url: 'https://example.com/fixture', verified: true, excerpt: '合成审核证据。' }] };
  try {
    await store.query("INSERT INTO slang_entries (id,status,current_version,payload,content_hash) VALUES (?,'candidate',1,?,?)", [id, JSON.stringify(payload), '0'.repeat(64)]);
    await store.query('UPDATE slang_meta SET generation=generation+1 WHERE id=1');
    assert.ok(!(await store.slangSnapshot()).entries.some(e => e.id === id));
    await store.query("UPDATE slang_entries SET status='published' WHERE id=?", [id]);
    await store.query('UPDATE slang_meta SET generation=generation+1 WHERE id=1');
    assert.ok((await store.slangSnapshot()).entries.some(e => e.id === id));
    const receipts = [{ message: 'fixture-message', entry: id }];
    await store.slangObserve(scope, receipts);
    assert.deepEqual(await store.slangPreferences(scope), {});
    await store.query('INSERT INTO slang_scopes (scope_key,enabled) VALUES (?,1)', [scope]);
    await store.slangObserve(scope, receipts); await store.slangObserve(scope, receipts);
    const [row] = await store.query('SELECT observations FROM slang_feedback WHERE scope_key=? AND entry_id=?', [scope, id]);
    assert.equal(row.observations, 1);
    assert.deepEqual(await store.slangPreferences('group:999'), {});
    const context = new SlangContext(store, scope, { slangBudget: 1800, slangTimeoutMs: 1000 });
    context.observe([{ messageId: 'fixture-message', text: payload.name }]);
    assert.match(await context.refresh(), /当前 fixture 的合成解释/);
  } finally { await store.close(); }
});
