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
// A heartbeat is a stand-in for work the chat can see taking a long time
// (looking something up, reading a page, handling a video). Ordinary social
// tools — reading unread, sending, waiting, memory — never justify one, even
// when the turn itself runs longer than the interval.
export const PROGRESS_TEXT_LIMIT = 400;
const SEARCH_TOOLS = new Set(['web_search', 'web_fetch', 'mcp__web-search-safe__web_search', 'mcp__web-search-safe__web_fetch']);
const SUBSTANTIVE_TOOLS = new Set(['qq_video_open', 'qq_video_probe', 'qq_video_extract_frames', 'qq_video_extract_audio', 'qq_video_transcode', 'qq_send_artifact']);

export function progressMode(config, model = config.model) {
  if (config.progressReports === 'off') return 'off';
  return config.progressModelsWithout?.has?.(model) ? 'heartbeat' : 'forward';
}

export function emptyProgress(turn, now = Date.now()) {
  return { turn, startedAt: now, steps: 0, commands: 0, searches: 0,
    substantive: false, quiet: false,
    lastForwardAt: 0, forwarded: 0, heartbeats: 0 };
}

/** Whether this tool is work a chat should hear about, rather than bookkeeping. */
export function isSubstantiveTool(name) {
  return SEARCH_TOOLS.has(name) || SUBSTANTIVE_TOOLS.has(name);
}

export function countToolCall(progress, name) {
  if (SEARCH_TOOLS.has(name)) progress.searches++;
  else progress.commands++;
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
