import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { settings } from '../src/policy.js';

const env = () => ({
  YUNYING_INTERNAL_SECRET: randomUUID(),
  YUNYING_ALLOW_GROUPS: '1001',
  YUNYING_ALLOW_PRIVATE: '2001',
  YUNYING_MODEL_PROVIDER: 'script',
  YUNYING_MODEL: 'script',
});

test('pre-sleep wait keeps the donor default and accepts a bounded test-group override', () => {
  assert.equal(settings(env()).preSleepWaitMs, 300000);
  assert.equal(settings({ ...env(), YUNYING_PRE_SLEEP_WAIT_MS: '60000' }).preSleepWaitMs, 60000);
  assert.equal(settings({ ...env(), YUNYING_PRE_SLEEP_WAIT_MS: '300000' }).preSleepWaitMs, 300000);
});

test('pre-sleep wait rejects values outside the donor observation bounds', () => {
  for (const value of ['59999', '300001', 'abc', '0', '-1']) {
    assert.throws(() => settings({ ...env(), YUNYING_PRE_SLEEP_WAIT_MS: value }), /整数参数超出范围|invalid/i, value);
  }
});
