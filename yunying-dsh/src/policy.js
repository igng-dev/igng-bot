import { timingSafeEqual } from 'node:crypto';
export class PolicyError extends Error {
  constructor(message = '拒绝访问：权限或来源不满足要求') { super(message); this.name = 'PolicyError'; }
}
export const canonicalKey = (input) => {
  const match = /^(group|private):([1-9][0-9]{0,19})$/.exec(String(input));
  if (!match) throw new PolicyError('无效会话 key');
  return `${match[1]}:${BigInt(match[2])}`;
};
export const equalSecret = (a, b) => typeof a === 'string' && typeof b === 'string' &&
  Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));
export const integer = (value, min, max, fallback) => value === undefined ? fallback :
  Number.isSafeInteger(Number(value)) && Number(value) >= min && Number(value) <= max ? Number(value) : (() => { throw new PolicyError('整数参数超出范围'); })();
export const bounded = (value, limit, required = true) => {
  if (typeof value !== 'string' || value.length > limit || (required && !value.trim())) throw new PolicyError('文本参数超出范围');
  return value;
};
export function settings(env = process.env) {
  const ids = (s = '') => new Set(s.split(/[,\s]+/).filter(s => /^[1-9][0-9]{0,19}$/.test(s)));
  if ((env.YUNYING_INTERNAL_SECRET || '').length < 32) throw new Error('YUNYING_INTERNAL_SECRET needs at least 32 characters');
  return {
    secret: env.YUNYING_INTERNAL_SECRET, adminSecret: env.YUNYING_ADMIN_SECRET || '',
    groups: ids(env.YUNYING_ALLOW_GROUPS), private: ids(env.YUNYING_ALLOW_PRIVATE),
    infraUrl: env.YUNYING_INFRA_URL || 'http://127.0.0.1:8788',
    host: env.YUNYING_DSH_HOST || '127.0.0.1', port: integer(env.YUNYING_DSH_PORT, 1, 65535, 8787),
    searchProvider: ['bing','deepseek'].includes(env.YUNYING_SEARCH_PROVIDER||'bing')?env.YUNYING_SEARCH_PROVIDER||'bing':(()=>{throw new Error('unsupported search provider');})(),
    provider: env.YUNYING_MODEL_PROVIDER || 'deepseek-official', model: env.YUNYING_MODEL || 'deepseek-v4-flash',
    maxTokens: integer(env.YUNYING_MAX_TOKENS, 256, 32768, 4096),
    nicknames: (env.YUNYING_NICKNAMES || '云萤,莹宝').split(',').map(s => s.trim()).filter(Boolean),
    batchWindowMs: 8000, preSleepWaitMs: 300000, minQuietMs: 10000,
    sendMinMs: 1000, sendMaxMs: 3000, maxMessageChars: 500,
    maxWakeMinute: 1, maxWakeHour: 12, maxSendMinute: 8, maxSendHour: 60,
    proactiveMinMs: 1800000, proactiveMaxMs: 5400000, proactiveIdleMs: 900000,
    proactiveProbability: .3, replyCheckMs: 30000,
  };
}
export function allowed(key, config) {
  const [kind, id] = canonicalKey(key).split(':');
  // Groups keep the operator allowlist. Private conversations are authorized by the
  // Python infrastructure against the IGNG account tier (plus/pro/admin); the profile
  // trusts the internal capability and no longer keeps a private allowlist.
  return kind === 'group' ? config.groups.has(id) : true;
}
export function authorize(state, args, exec) {
  if (!exec?.agent || exec.agent.id !== state.sessionId || canonicalKey(args.key) !== state.key || !equalSecret(args.token, state.agentToken)) throw new PolicyError();
}
export function safeNetworkQuery(query) {
  bounded(query, 300);
  // No automatic slang research; searches are explicit agent actions. Secrets are never accepted as queries.
  if (/\b(?:sk-[a-zA-Z0-9_-]{12,}|Bearer\s+\S+|password\s*[=:]|api[_ -]?key\s*[=:])/i.test(query)) throw new PolicyError('搜索包含凭据形态');
  return query;
}
