#!/usr/bin/env node
// Read-only preflight for the compaction policy used by the YunYing profile.
// Supports both the bundle YAML and the operator JSON patch.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../yunying-dsh/package.json', import.meta.url));
const yaml = require('js-yaml');

const CONTEXT_WINDOW = 262144;
const RESERVED_COMPLETION_TOKENS = 8192;
const MIN_THRESHOLD = 90000;
const MIN_MARGIN = 40000;

export function inspectConfig(rows) {
  const list = Array.isArray(rows) ? rows : [rows];
  const compaction = list.find(row => row && row.id === 'compaction-basic');
  if (!compaction) throw new Error('compaction-basic is missing');
  const config = compaction.config || {};
  const ratio = Number(config.thresholdRatio);
  const headroom = Number(config.headroomTokens);
  const retain = Number(config.retainTokens);
  if (!Number.isFinite(ratio) || ratio <= 0 || ratio > 1) throw new Error('thresholdRatio must be in (0, 1]');
  if (!Number.isInteger(headroom) || headroom < 0) throw new Error('headroomTokens must be a non-negative integer');
  if (!Number.isInteger(retain) || retain <= 0) throw new Error('retainTokens must be a positive integer');
  const pressureBudget = CONTEXT_WINDOW - RESERVED_COMPLETION_TOKENS - headroom;
  if (pressureBudget <= 0) throw new Error('headroom leaves no pressure budget');
  const threshold = Math.floor(Math.min(CONTEXT_WINDOW * ratio, pressureBudget));
  const margin = threshold - retain;
  if (threshold < MIN_THRESHOLD) throw new Error(`threshold ${threshold} is below ${MIN_THRESHOLD}`);
  if (retain >= threshold) throw new Error(`retainTokens ${retain} must be below threshold ${threshold}`);
  if (margin < MIN_MARGIN) throw new Error(`retention margin ${margin} is below ${MIN_MARGIN}`);
  return { contextWindow: CONTEXT_WINDOW, thresholdRatio: ratio, headroomTokens: headroom,
    retainTokens: retain, thresholdTokens: threshold, marginTokens: margin };
}

function load(path) {
  const text = readFileSync(path, 'utf8').trimStart();
  if (text.startsWith('[') || text.startsWith('{')) return JSON.parse(text);
  return yaml.load(text);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const path = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../yunying-dsh/cordis.patch.yml');
  try {
    const result = inspectConfig(load(path));
    console.log(`compaction config OK: ${path}`);
    console.log(JSON.stringify(result, null, 2));
  } catch (error) {
    console.error(`compaction config FAILED: ${path}`);
    console.error(error.message);
    process.exitCode = 1;
  }
}
