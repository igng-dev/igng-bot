// Usage: python -m igngbot_v4.slang.seed --evaluation | node scripts/slang-evaluate.mjs
import { rankSlang } from '../yunying-dsh/src/slang.js';
let raw = '';
for await (const chunk of process.stdin) { raw += chunk; if (raw.length > 2_000_000) throw new Error('dataset too large'); }
const data = JSON.parse(raw);
let tp = 0, fp = 0, fn = 0;
const failures = [];
for (const item of data.cases) {
  const actual = rankSlang(data.entries, item.text).matches.map(entry => entry.id);
  tp += actual.filter(id => item.expected.includes(id)).length;
  fp += actual.filter(id => !item.expected.includes(id)).length;
  fn += item.expected.filter(id => !actual.includes(id)).length;
  if (actual.sort().join() !== item.expected.slice().sort().join()) failures.push({ id: item.id, expected: item.expected, actual });
}
console.log(JSON.stringify({ warning: data.warning, cases: data.cases.length, precision: tp / (tp + fp || 1), recall: tp / (tp + fn || 1), failures }, null, 2));
if (failures.length) process.exitCode = 1;
