// Filtered, queryable projection of the official DSH Session log.
//
// The JSONL backend stays authoritative for durability and recovery. This module
// reads the same `session/event` stream the accounting adapter already observes
// and stores the model-visible flow — turns, user/assistant/system messages, tool
// calls and a tool-result marker — in MySQL so a conversation can be queried and
// reconstructed without the official volume. Disposable payloads (assistant raw
// streams, tool-result bodies, request tool schemas, inbox splices) are
// deliberately dropped; the official log still holds the exact bytes, so the
// whole table can be rebuilt from it with INSERT IGNORE.
import { createHash } from 'node:crypto';

const TEXT_LIMIT = 65536;   // model-visible text kept per message
const JSON_LIMIT = 16384;   // tool-call arguments and structured envelopes
const PREVIEW_LIMIT = 2048;
const TOOL_RESULT_MODES = new Set(['none', 'preview', 'full']);

/** Bound applied to any single message body; exported so tests can assert it. */
export const transcriptTextLimit = TEXT_LIMIT;

// Tool results are dropped by design. `preview`/`full` exist only for an
// operator who temporarily needs more; neither is the default.
export function toolResultMode(env = process.env) {
  const value = String(env.YUNYING_SESSION_TOOL_RESULTS || 'none').trim().toLowerCase();
  return TOOL_RESULT_MODES.has(value) ? value : 'none';
}

const text = content => (Array.isArray(content) ? content : [])
  .filter(item => item && (item.type === 'text' || item.type === 'reasoning') && typeof item.text === 'string')
  .map(item => item.text).join('\n');

const bound = (value, limit) => typeof value !== 'string' || !value
  ? { value: null, truncated: false }
  : (value.length <= limit ? { value, truncated: false } : { value: value.slice(0, limit), truncated: true });

const sha256 = value => createHash('sha256').update(value).digest('hex');

const media = content => (Array.isArray(content) ? content : [])
  .filter(item => item && (item.type === 'image' || item.type === 'file'))
  .map(item => ({
    type: item.type,
    ...(item.attachment?.attachmentId ? { attachmentId: item.attachment.attachmentId } : {}),
    ...(item.attachment?.mediaType ? { mediaType: item.attachment.mediaType } : {})
  }));

const toolCalls = content => (Array.isArray(content) ? content : [])
  .filter(item => item && item.type === 'tool-call')
  .map(item => ({ id: item.id, name: item.name }));

/**
 * Project one Session event into a bounded transcript row, or `null` when the
 * event is deliberately not persisted. Unknown plugin events return `null`
 * rather than failing the Agent loop.
 */
export function projectEvent(sessionId, event, mode = 'none') {
  if (!sessionId || !event || typeof event.type !== 'string' || !Number.isSafeInteger(Number(event.seq))) return null;
  const data = event.data || {};
  const row = {
    dsh_session_id: String(sessionId).slice(0, 80),
    event_seq: Number(event.seq),
    event_type: event.type.slice(0, 40),
    turn: Number.isInteger(data.turn) ? data.turn : null,
    step: Number.isInteger(data.step) ? data.step : null,
    role: null, content: null, data: null, content_sha256: null, truncated: 0,
    event_time: Number.isFinite(event.time) ? new Date(event.time) : null
  };
  let truncated = false;
  switch (event.type) {
    case 'system/message': {
      row.role = 'system';
      const body = bound(text(data.message?.content), TEXT_LIMIT);
      row.content = body.value; truncated = body.truncated;
      row.data = { kind: data.message?.source?.kind ?? 'system-prompt' };
      break;
    }
    case 'developer/message': {
      row.role = 'developer';
      const body = bound(text(data.message?.content), TEXT_LIMIT);
      row.content = body.value; truncated = body.truncated;
      row.data = { kind: data.message?.source?.kind ?? null };
      break;
    }
    case 'user/message': {
      row.role = 'user';
      const body = bound(text(data.content), TEXT_LIMIT);
      row.content = body.value; truncated = body.truncated;
      const attached = media(data.content);
      row.data = { messageId: data.id ?? null, source: data.source?.kind ?? 'user',
        ...(attached.length ? { attachments: attached } : {}) };
      break;
    }
    case 'assistant/message': {
      row.role = 'assistant';
      const body = bound(text(data.message?.content), TEXT_LIMIT);
      row.content = body.value; truncated = body.truncated;
      const calls = toolCalls(data.message?.content);
      row.data = { model: data.message?.source?.model ?? null, provider: data.message?.source?.provider ?? null,
        ...(data.usage ? { usage: data.usage } : {}),
        ...(data.interrupted ? { interrupted: true } : {}),
        ...(calls.length ? { toolCalls: calls } : {}) };
      break;
    }
    case 'assistant/attempt':
      // The raw failed stream is dropped; the official log retains it.
      row.role = 'assistant';
      row.data = { failed: true };
      break;
    case 'tool/call': {
      row.role = 'tool';
      // `arguments` is the raw JSON string the model produced; keep it verbatim.
      const body = bound(data.arguments, JSON_LIMIT);
      row.content = body.value; truncated = body.truncated;
      row.data = { callId: data.callId ?? null, name: data.name ?? null };
      break;
    }
    case 'tool/result': {
      row.role = 'tool';
      const body = text(data.message?.content);
      const envelope = { callId: data.message?.toolCallId ?? null,
        isError: data.message?.isError === true, bytes: Buffer.byteLength(body, 'utf8') };
      if (mode === 'preview' || mode === 'full') {
        const kept = bound(body, mode === 'full' ? TEXT_LIMIT : PREVIEW_LIMIT);
        row.content = kept.value; truncated = kept.truncated;
        envelope.stored = kept.value ? mode : 'empty';
      } else {
        envelope.stored = 'dropped';
      }
      row.data = envelope;
      break;
    }
    case 'request/header': {
      const config = data.header?.config || {};
      row.data = { reason: data.reason ?? null, provider: config.provider ?? null, model: config.model ?? null,
        ...(Number.isInteger(config.maxTokens) ? { maxTokens: config.maxTokens } : {}) };
      break;
    }
    case 'request/context':
      row.data = { provider: data.provider ?? null, model: data.model ?? null,
        ...(Number.isInteger(data.contextWindow) ? { contextWindow: data.contextWindow } : {}) };
      break;
    case 'compaction/summary': {
      row.role = 'assistant';
      const body = bound(text(data.summary), TEXT_LIMIT);
      row.content = body.value; truncated = body.truncated;
      break;
    }
    case 'turn/start':
    case 'turn/end':
    case 'step/start':
    case 'step/end':
    case 'compaction/start':
    case 'compaction/end':
    case 'session/end-seed':
      if (event.type === 'turn/end') row.data = { reason: data.reason?.kind ?? null };
      break;
    default:
      return null;
  }
  if (row.content) row.content_sha256 = sha256(row.content);
  if (row.data) {
    const encoded = JSON.stringify(row.data);
    if (encoded.length > JSON_LIMIT) { row.data = { truncated: true, keys: Object.keys(row.data) }; truncated = true; }
  }
  row.truncated = truncated ? 1 : 0;
  return row;
}

/**
 * Read-side counterpart of {@link projectEvent}: stored rows in, ordered,
 * model-visible flow out.
 */
export function reconstruct(rows) {
  return [...(Array.isArray(rows) ? rows : [])]
    .sort((left, right) => Number(left.event_seq) - Number(right.event_seq))
    .map(row => ({
      seq: Number(row.event_seq),
      type: row.event_type,
      turn: row.turn ?? null,
      step: row.step ?? null,
      role: row.role || null,
      text: row.content || '',
      data: typeof row.data === 'string' ? JSON.parse(row.data || 'null') : (row.data ?? null),
      truncated: !!row.truncated,
      time: row.event_time ?? null
    }));
}
