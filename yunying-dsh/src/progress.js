// Progress reporting for long social turns.
//
// Two modes, chosen by configuration:
//  - `forward`: the model emits text on intermediate steps (text + tool calls).
//    That text is sent to QQ as a progress note, rate-limited per turn.
//  - `heartbeat`: the configured model does not emit interim text, so a bounded
//    timer sends a command/search/step summary while the turn is still running.
// Final answers stay tool-only: assistant text without tool calls is never
// auto-sent, so the model keeps sole ownership of what is a real reply.
//
// A silent model can also call the dedicated `progress_note` tool before a
// substantive action. That note is user-facing progress, not a reply, and gets
// the same permission/ledger treatment as forwarded text. The heartbeat remains
// a fallback when the model forgets or the tool is unavailable.
//
// A heartbeat is a stand-in for work the chat can see taking a long time
// (looking something up, reading a page, handling a video). Ordinary social
// tools — reading unread, sending, waiting, memory — never justify one, even
// when the turn itself runs longer than the interval.
export const PROGRESS_TEXT_LIMIT = 400;
export const PROGRESS_NOTE_LIMIT = 80;
export const PROGRESS_NOTE_TOOL = 'progress_note';
const SEARCH_TOOLS = new Set(['web_search', 'web_fetch', 'mcp__web-search-safe__web_search', 'mcp__web-search-safe__web_fetch']);
const STYLE_TOOLS = new Set(['style_read', 'style_patch']);
const SUBSTANTIVE_TOOLS = new Set(['qq_video_open', 'qq_video_probe', 'qq_video_extract_frames', 'qq_video_extract_audio', 'qq_video_transcode', 'qq_send_artifact']);

export function progressMode(config, model = config.model) {
  if (config.progressReports === 'off') return 'off';
  return config.progressModelsWithout?.has?.(model) ? 'heartbeat' : 'forward';
}

/** Whether the silent-model path should advertise the dedicated progress tool. */
export function progressNoteEnabled(config, model = config.model) {
  return progressMode(config, model) === 'heartbeat';
}

export function emptyProgress(turn, now = Date.now()) {
  return { turn, startedAt: now, steps: 0, commands: 0, searches: 0,
    substantive: false, quiet: false, styleOnly: null,
    lastForwardAt: 0, forwarded: 0, heartbeats: 0, notes: 0, lastNoteAt: 0 };
}

/** A short, user-facing note from the dedicated progress tool. */
export function isProgressNoteTool(name) {
  return name === PROGRESS_NOTE_TOOL;
}

export function progressNoteText(value) {
  const text = typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
  if (text.length < 2) return null;
  return text.length <= PROGRESS_NOTE_LIMIT ? text : text.slice(0, PROGRESS_NOTE_LIMIT - 1) + '…';
}

/** A fresh note holds ordinary heartbeats for a while to avoid duplicate updates. */
export function heartbeatAllowed(progress, config, now = Date.now()) {
  if (!progress.notes) return true;
  return now - progress.lastNoteAt >= (Number(config.progressNoteHoldMs) || 0);
}

/** Whether this tool is work a chat should hear about, rather than bookkeeping. */
export function isSubstantiveTool(name) {
  return SEARCH_TOOLS.has(name) || SUBSTANTIVE_TOOLS.has(name);
}

export function isStyleTool(name) {
  return STYLE_TOOLS.has(name);
}

export function countToolCall(progress, name) {
  // The note is a reporting action, not evidence of work the chat should see
  // announced again by a heartbeat.
  if (isProgressNoteTool(name)) return;
  if (isStyleTool(name)) progress.styleOnly = progress.styleOnly !== false;
  else progress.styleOnly = false;
  if (SEARCH_TOOLS.has(name)) progress.searches++;
  else if (!isStyleTool(name)) progress.commands++;
  if (isSubstantiveTool(name)) progress.substantive = true;
}

/** A real reply ends the excuse for a heartbeat; the answer is already in the chat. */
export function isReplyTool(name) {
  return name === 'qq_send_message' || name === 'qq_send_burst' || name === 'qq_reply'
    || name === 'mcp__snowluma__qq_send_message' || name === 'mcp__snowluma__qq_reply';
}

/** Interim text for forwarding, or null when there is nothing worth sending. */
export function forwardableText(content) {
  const text = (Array.isArray(content) ? content : [])
    .filter(item => item && item.type === 'text' && typeof item.text === 'string')
    .map(item => item.text).join('\n').trim();
  if (text.length < 4) return null;
  return text.length <= PROGRESS_TEXT_LIMIT ? text : text.slice(0, PROGRESS_TEXT_LIMIT - 1) + '…';
}

export function heartbeatText(progress, now = Date.now()) {
  const seconds = Math.max(0, Math.round((now - progress.startedAt) / 1000));
  if (progress.searches) return `正在查资料（已 ${seconds} 秒）：搜了 ${progress.searches} 次。`;
  return `还在处理（已 ${seconds} 秒），稍等。`;
}
