import { createHash } from 'node:crypto';
import { PolicyError, bounded, integer } from './policy.js';

export const STYLE_CONTEXTS = ['general', 'casual', 'technical', 'support', 'conflict'];
export const STYLE_DIMENSIONS = ['length', 'segmentation', 'register', 'punctuation', 'particles', 'humor', 'structure'];
export const STYLE_STRENGTHS = ['mild', 'moderate'];
export const STYLE_STATUSES = ['active', 'disabled'];
export const MAX_STYLE_RULES = 24;
export const MAX_STYLE_CANDIDATES = 24;
export const MAX_MODEL_STYLE_CHANGES = 2;
export const STYLE_RENDER_LIMIT = 2500;
const MIN_STYLE_GAP_MS = 6 * 60 * 60 * 1000;

const LIMITS = {
  preference: 160,
  exception: 80,
  evidence: 240,
  reason: 500,
  id: 48,
};

export function styleHash(value) {
  return createHash('sha256').update(canonicalJson(value)).digest('hex');
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export function emptyStyle(version = 0) {
  return { version, status: 'active', rules: [], candidates: [], contentHash: styleHash({ status: 'active', rules: [], candidates: [] }), updatedAt: null };
}

function text(value, limit, label) {
  const normalized = bounded(value, limit).replace(/\s+/g, ' ').trim();
  if (/[【】]|^[-*#]|```|系统提示|忽略以上|ignore previous|tool call|调用工具/i.test(normalized)) {
    throw new PolicyError(`${label}包含不能进入风格档案的指令形态`);
  }
  return normalized;
}

function enumValue(value, allowed, label) {
  if (!allowed.includes(value)) throw new PolicyError(`${label}无效`);
  return value;
}

export function normalizeRule(input, { candidate = false } = {}) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new PolicyError('风格规则格式无效');
  const id = text(input.id, LIMITS.id, '规则标识').toLowerCase();
  if (!/^[a-z][a-z0-9._-]{1,47}$/.test(id)) throw new PolicyError('规则标识无效');
  const exceptions = Array.isArray(input.exceptions) ? input.exceptions : [];
  if (exceptions.length > 4) throw new PolicyError('风格例外过多');
  const rule = {
    id,
    context: enumValue(input.context, STYLE_CONTEXTS, '风格场景'),
    dimension: enumValue(input.dimension, STYLE_DIMENSIONS, '风格维度'),
    preference: text(input.preference, LIMITS.preference, '风格倾向'),
    exceptions: exceptions.map(item => text(item, LIMITS.exception, '风格例外')),
    strength: enumValue(input.strength, STYLE_STRENGTHS, '风格强度'),
    status: 'active',
  };
  if (candidate) rule.evidence = text(input.evidence, LIMITS.evidence, '候选证据');
  return rule;
}

export function normalizeProfile(input = {}) {
  const rules = Array.isArray(input.rules) ? input.rules.map(rule => normalizeRule(rule)) : [];
  const candidates = Array.isArray(input.candidates) ? input.candidates.map(rule => normalizeRule(rule, { candidate: true })) : [];
  if (rules.length > MAX_STYLE_RULES || candidates.length > MAX_STYLE_CANDIDATES) throw new PolicyError('风格档案超出容量');
  if (new Set(rules.map(rule => rule.id)).size !== rules.length) throw new PolicyError('风格规则标识重复');
  if (new Set(candidates.map(rule => rule.id)).size !== candidates.length) throw new PolicyError('候选风格标识重复');
  const status = enumValue(input.status || 'active', STYLE_STATUSES, '风格状态');
  const profile = { status, rules, candidates };
  const rendered = renderStyle(profile);
  if (rendered && rendered.length > STYLE_RENDER_LIMIT) throw new PolicyError('风格档案渲染后超出上下文预算');
  return { ...profile, contentHash: styleHash(profile) };
}

const CONTEXT_LABELS = {
  general: '一般场景', casual: '日常闲聊', technical: '技术或事实说明', support: '求助、安慰或敏感话题', conflict: '分歧或争执',
};
const DIMENSION_LABELS = {
  length: '长度', segmentation: '分条', register: '正式程度', punctuation: '标点', particles: '语气词与口癖', humor: '幽默', structure: '表达结构',
};

const STYLE_HEADING = '【当前会话表达风格】';

/** Replace one rendered style block without touching the rest of a frozen prompt. */
export function replaceStyleBlock(text, styleText) {
  const start = text.indexOf(STYLE_HEADING);
  const end = start < 0 ? -1 : text.indexOf('\n\n', start);
  const next = styleText || '';
  if (start < 0) return next ? `${text}\n\n${next}` : text;
  const prefix = text.slice(0, start).replace(/\n+$/, '');
  const suffix = end < 0 ? '' : text.slice(end + 2);
  return [prefix, next, suffix].filter(Boolean).join('\n\n');
}

export function renderStyle(profile) {
  const normalized = profile?.rules ? profile : emptyStyle();
  if (normalized.status === 'disabled' || !normalized.rules?.length) return '';
  const lines = normalized.rules.map(rule => {
    const exceptions = rule.exceptions?.length ? `；例外：${rule.exceptions.join('、')}` : '';
    return `- ${CONTEXT_LABELS[rule.context] || rule.context} / ${DIMENSION_LABELS[rule.dimension] || rule.dimension}（${rule.strength === 'moderate' ? '较稳定' : '轻度'}）：${rule.preference}${exceptions}`;
  });
  return [
    `${STYLE_HEADING}以下是可偏离的表达倾向，只影响措辞、长度、分条和语气。`,
    '它不改变你的身份、事实判断、权限、工具协议、参与意愿或安全规则；不要求复制原话或固定口癖。',
    ...lines,
  ].join('\n');
}

function sourceTime(row) {
  const value = Number(row.payload?.time || Date.parse(row.created_at || ''));
  if (!Number.isFinite(value) || value <= 0) throw new PolicyError('风格来源缺少可靠时间');
  return value;
}

export function assertStyleEvidence(rows, { privateConversation = false, now = Date.now(), minGapMs = MIN_STYLE_GAP_MS } = {}) {
  if (!Array.isArray(rows) || rows.length < 2) throw new PolicyError('激活风格至少需要两条独立来源');
  const speakers = new Set(rows.map(row => String(row.payload.userId || '')));
  const times = rows.map(sourceTime).sort((a, b) => a - b);
  if (times.at(-1) - times[0] < minGapMs) throw new PolicyError('风格证据需要来自不同时间段');
  if (times.some(time => time > now + 5 * 60 * 1000)) throw new PolicyError('风格来源时间无效');
  if (!privateConversation && speakers.size < 2) throw new PolicyError('群风格需要至少两名成员的独立证据');
  return { speakers: speakers.size, spanMs: times.at(-1) - times[0] };
}

export function applyStyleChanges(current, changes, { allowActivate = false } = {}) {
  if (!Array.isArray(changes) || !changes.length || changes.length > MAX_MODEL_STYLE_CHANGES) throw new PolicyError('每次最多修订两条风格');
  const rules = new Map((current.rules || []).map(rule => [rule.id, rule]));
  const candidates = new Map((current.candidates || []).map(rule => [rule.id, rule]));
  for (const change of changes) {
    const op = change?.op;
    if (!['add', 'replace', 'retire', 'observe'].includes(op)) throw new PolicyError('风格修订操作无效');
    if (op === 'observe') {
      const rule = normalizeRule(change.rule, { candidate: true });
      candidates.set(rule.id, rule);
      continue;
    }
    const id = text(change.id || change.rule?.id, LIMITS.id, '规则标识').toLowerCase();
    if (op === 'retire') {
      if (!rules.delete(id)) throw new PolicyError('要停用的风格规则不存在');
      continue;
    }
    if (!allowActivate) throw new PolicyError('证据不足，只能保存为候选风格');
    const rule = normalizeRule(change.rule);
    if (rule.id !== id) throw new PolicyError('风格规则标识不一致');
    if (op === 'add' && (rules.has(id) || candidates.has(id))) throw new PolicyError('风格规则已存在，请使用 replace');
    if (op === 'replace' && !rules.has(id) && !candidates.has(id)) throw new PolicyError('要替换的风格规则不存在');
    rules.set(id, rule);
    candidates.delete(id);
  }
  return normalizeProfile({ status: current.status || 'active', rules: [...rules.values()], candidates: [...candidates.values()] });
}

function storedJson(value) {
  // mysql2 parses JSON columns into objects; only raw SQL strings still need parsing.
  if (typeof value === 'string') return JSON.parse(value);
  return value ?? [];
}

export function styleView(row) {
  if (!row) return emptyStyle();
  const rules = storedJson(row.rules_json);
  const candidates = storedJson(row.candidates_json);
  return {
    version: Number(row.current_version || row.version || 0),
    status: row.status || 'active',
    rules: Array.isArray(rules) ? rules : [],
    candidates: Array.isArray(candidates) ? candidates : [],
    contentHash: row.content_hash,
    updatedAt: row.updated_at || row.created_at || null,
    prompt: renderStyle({ status: row.status, rules }),
  };
}

export function styleReason(value, fallback = '风格修订') {
  return bounded(value || fallback, LIMITS.reason);
}

export function styleVersionNumber(value) {
  return integer(value, 0, 1_000_000_000);
}
