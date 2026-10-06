// Progress reporting for long social turns.
//
// Two modes, chosen by configuration:
//  - `forward`: the model emits text on intermediate steps (text + tool calls).
//    That text is sent to QQ as a progress note, rate-limited per turn.
//  - `heartbeat`: the configured model does not emit interim text, so a bounded
//    timer sends a command/search/step summary while the turn is still running.
// Final answers stay tool-only: assistant text without tool calls is never
// auto-sent, so the model keeps sole ownership of what is a real reply.
export const PROGRESS_TEXT_LIMIT = 400;
const SEARCH_TOOLS = new Set(['web_search', 'web_fetch']);

export function progressMode(config, model = config.model) {
  if (config.progressReports === 'off') return 'off';
  return config.progressModelsWithout?.has?.(model) ? 'heartbeat' : 'forward';
}

export function emptyProgress(turn, now = Date.now()) {
  return { turn, startedAt: now, steps: 0, commands: 0, searches: 0,
    lastForwardAt: 0, forwarded: 0, heartbeats: 0 };
}

export function countToolCall(progress, name) {
  if (SEARCH_TOOLS.has(name)) progress.searches++;
  else progress.commands++;
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
  if (!progress.commands && !progress.searches && !progress.steps) return `任务进行中（已 ${seconds} 秒）：正在思考…`;
  return `任务进行中（已 ${seconds} 秒）：运行了 ${progress.commands} 条命令、${progress.searches} 次搜索、${progress.steps} 次思考。`;
}
