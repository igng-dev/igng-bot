'use strict';
const byId = id => document.getElementById(id);
const examples = {
  save: { entry: { id: 'example:term', name: '示例', meaning: '待核验', aliases: ['示例'], sources: [{ id: 'example:source', url: 'https://example.com/source', excerpt: '', verified: false }] }, status: 'candidate', expectedVersion: 0, reason: '说明核验过程' },
  review: { id: '候选 UUID', expectedVersion: 0, reason: '已核对引用材料', publish: true },
  rollback: { id: 'example:term', version: 1, expectedVersion: 2, reason: '回滚原因' },
  'source-save': { source: { id: 'example:creator', kind: 'json', creator_id: 'example', feed_url: 'https://example.com/feed.json', allowed_hosts: ['example.com'], enabled: false, media_enabled: false, poll_seconds: 21600 } },
  create: { source_id: 'owner-import', material: { url: 'https://example.com/video', title: '已获授权材料', origin: 'example-creator', segments: [{ id: 'segment:0', start: 0, end: 10, excerpt: '真实字幕原文' }] } },
  poll: { id: 'example:creator' }, retry: { id: '任务 UUID' },
  policy: { policy: { recognition: true, recommendation: true, maintenance: false, auto_publish: false } },
  feedback: { scope: 'group:1001', id: 'example:term', preference: 'avoid' },
  consent: { scope: 'group:1001', enabled: false },
  'semantic-consent': { scope: 'group:1001', enabled: false },
  'resolve-unavailable-session': { session: '确实丢失的原生 Session UUID', reason: '先核查 DSH_HOME、备份和原生记录；不是直接跳过恢复', acknowledge_unknown_usage: false },
};
byId('examples').textContent = JSON.stringify(examples, null, 2);
function edit(action, data) { byId('action').value = action; byId('editor').value = JSON.stringify(data, null, 2); byId('reviewed').checked = false; }
edit('save', examples.save);
byId('action').addEventListener('change', () => edit(byId('action').value, examples[byId('action').value]));
byId('forget').addEventListener('click', () => { byId('secret').value = ''; byId('results').replaceChildren(); byId('message').textContent = '凭据已清除'; });
async function call(action, data = {}) {
  const token = byId('secret').value;
  if (token.length < 32) throw new Error('需要有效 Owner 凭据');
  const response = await fetch('/admin/' + action, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token }, body: JSON.stringify(data), credentials: 'omit', redirect: 'error' });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求被拒绝');
  return value;
}
async function perform(work) {
  byId('message').textContent = '处理中…';
  try { await work(); byId('message').textContent = '操作成功'; }
  catch (error) { byId('message').textContent = error.message; }
}
function button(parent, label, work) { const element = document.createElement('button'); element.textContent = label; element.addEventListener('click', () => perform(work)); parent.append(element); }
function display(action, result) {
  const container = byId('results'); container.replaceChildren();
  const rows = result.entries || result.candidates || result.sources || result.jobs;
  for (const row of rows || [result]) {
    const article = document.createElement('article'), pre = document.createElement('pre');
    pre.textContent = JSON.stringify(row, null, 2); article.append(pre); container.append(article);
    if (action === 'list') {
      button(article, '编辑', async () => edit('save', { entry: row, status: row.status, expectedVersion: row.version, reason: '' }));
      button(article, '版本历史', async () => display('versions', await call('versions', { id: row.id })));
    }
    if (action === 'candidates') {
      button(article, '核对并审核', async () => edit('review', { id: row.id, expectedVersion: 0, reason: '', publish: true }));
      button(article, '复制到编辑区修订', async () => edit('save', { entry: row.payload, status: 'candidate', expectedVersion: 0, reason: '' }));
    }
    if (action === 'sources') button(article, '编辑来源', async () => edit('source-save', { source: row.config }));
    if (action === 'jobs' && row.status === 'failed') button(article, '准备重试', async () => edit('retry', { id: row.id }));
    if (action === 'policy') button(article, '编辑开关', async () => edit('policy', { policy: row.policy }));
  }
  if(action==='list'&&result.entries?.length>=100)button(container,'下一页',async()=>display('list',await call('list',{after:result.after})));
}
byId('navigation').addEventListener('click', event => { const action = event.target.dataset.action; if (action) perform(async () => display(action, await call(action))); });
byId('submit').addEventListener('click', () => perform(async () => {
  if (!byId('reviewed').checked) throw new Error('提交前请核对并勾选授权');
  const result = await call(byId('action').value, JSON.parse(byId('editor').value)); display('result', result); byId('reviewed').checked = false;
}));
byId('export').addEventListener('click', () => perform(async () => {
  const entries = []; let after = '';
  for (;;) { const page = await call('list', { after, limit: 200 }); if (!page.entries.length) break; entries.push(...page.entries); after = page.after; }
  const url = URL.createObjectURL(new Blob([JSON.stringify({ entries }, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url; link.download = 'slang-export.json'; link.click(); URL.revokeObjectURL(url);
}));
