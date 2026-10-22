import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';
import { inspectConfig } from '../../scripts/check-yunying-compaction-config.mjs';

const patchPath = new URL('../cordis.patch.yml', import.meta.url);
const patch = yaml.load(readFileSync(patchPath, 'utf8'));
const compaction = patch.find(row => row && row.id === 'compaction-basic');

test('compaction policy leaves real headroom after the CJK retention floor', () => {
  assert.ok(compaction, 'compaction-basic is mounted');
  const result = inspectConfig(patch);
  assert.equal(result.contextWindow, 262144);
  assert.equal(result.thresholdRatio, 0.4);
  assert.equal(result.retainTokens, 8192);
  assert.ok(result.thresholdTokens >= 100000);
  assert.ok(result.marginTokens >= 40000);
});

test('compaction preflight CLI validates the bundle patch', () => {
  const script = fileURLToPath(new URL('../../scripts/check-yunying-compaction-config.mjs', import.meta.url));
  const output = execFileSync(process.execPath, [script, fileURLToPath(patchPath)], { encoding: 'utf8' });
  assert.match(output, /compaction config OK/);
  assert.match(output, /"thresholdTokens": 104857/);
});

test('compaction preflight rejects the old 0.2 policy that thrashed every step', () => {
  const old = [{ id: 'compaction-basic', config: { thresholdRatio: 0.2, headroomTokens: 16384, retainTokens: 16384, maxTokens: 16384 } }];
  assert.throws(() => inspectConfig(old), /threshold 52428 is below 90000/);
});
