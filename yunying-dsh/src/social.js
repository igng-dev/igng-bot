// Reserved2 state/watermark/wait semantics ported from Derpyu520/qq-bridge (MIT).
// Durable event storage and native cancellation are YunYing adapters; see donor/qq-bridge/PROVENANCE.md.
import { randomBytes } from 'node:crypto';
import { setTimeout as sleep } from 'node:timers/promises';
import { compactModelMessage } from '../donor/qq-bridge/qq-model-view.js';
import { looksLikeUnfinished } from '../donor/qq-bridge/v2-wait.js';
import { PolicyError, integer } from './policy.js';
export const EXPLICIT_END_RE = /(?:不聊了|不说了|晚安|睡了|先睡了|下了|先下了|拜拜|再见|走了|先走|撤了|去忙|忙了|下次再聊|下次聊|散了吧|结束|就到这|先这样|就这样吧|886|88|睡觉了|下班了|去洗澡|去吃饭了)/i;
export const defaultWakeConfig = (now = Date.now()) => ({
  mode: 'diving', infinite: true, sleepUntil: null,
  triggers: { atMention: true, nameMention: true, question: true, poke: true, anyMessage: false,
    probability: .20, speakerIds: [], keywords: [] },
  batchWindowMs: 8000, lastWakeAt: 0, wakeCount: 0, noActionCount: 0, confirmedAt: 0, confirmedBy: 'default',
});
export function normalizeSpeakerIds(values) {
  return [...new Set((Array.isArray(values) ? values : []).map(String).filter(s => /^[1-9][0-9]{0,19}$/.test(s)))].slice(0, 20);
}
export class SocialState {
  constructor(key, sessionId, saved = {}, config = {}, clock = () => Date.now()) {
    this.key = key; this.sessionId = sessionId; this.config = config; this.clock = clock;
    this.agentToken = saved.agentToken || randomBytes(16).toString('hex');
    this.wakeConfig = saved.wakeConfig || defaultWakeConfig();
    // The old 0.05 default is migrated once to the new 0.20; deliberate custom values stay.
    if (this.wakeConfig?.triggers?.probability === 0.05) this.wakeConfig.triggers.probability = 0.20;
    for (const name of ['lastIncomingAt', 'lastAiReplyAt', 'lastActionAt', 'lastReadThroughSeq', 'preSleepWaitSatisfiedAt', 'preSleepWaitObservedAt', 'lastProactiveAt']) this[name] = Number(saved[name]) || 0;
    this.bootstrapSent = !!saved.bootstrapSent; this.lastWakeReason = saved.lastWakeReason || '';
    this.activeTopics = saved.activeTopics || []; this.pendingThoughts = saved.pendingThoughts || [];
    this.memberImpressions = saved.memberImpressions || {};
    this.wakeTimes = saved.wakeTimes || []; this.sendTimes = saved.sendTimes || [];
    this.chatMode = true; this.recentMessages = []; this.unread = []; this.modelSeenSeqs = new Set();
    this.lastUnreadSeq = 0; this.waiting = false;
    this.ensureWakeable();
  }
  snapshot() {
    const result = {};
    for (const name of ['agentToken','wakeConfig','lastIncomingAt','lastAiReplyAt','lastActionAt','lastReadThroughSeq',
      'preSleepWaitSatisfiedAt','preSleepWaitObservedAt','bootstrapSent','lastWakeReason','activeTopics','pendingThoughts',
      'memberImpressions','wakeTimes','sendTimes','lastProactiveAt']) result[name] = this[name];
    return result;
  }
  append(event, replay = false) {
    const seq = Number(event.seq); if (seq<=this.lastUnreadSeq) return; this.lastUnreadSeq = Math.max(this.lastUnreadSeq, seq);
    const msg = { ...event.payload, seq, time: Number(event.payload.time) || this.clock() };
    msg.eventId=event.event_id; delete msg.key;
    if (msg.kind === 'recall') {
      for (const list of [this.unread, this.recentMessages]) for (const old of list) {
        if (old.messageId === msg.messageId) { old.text = '[消息已撤回]'; old.plain = ''; old.media = []; old.structure = []; old.isRecalled = true; }
      }
    }
    this.recentMessages.push(msg);
    this.recentMessages = this.recentMessages.slice(-100);
    // All received events have durable sequence numbers, including echoes and recalls. Never skip a gap.
    if (seq > this.lastReadThroughSeq) this.unread.push(msg);
    this.unread.sort((a, b) => a.seq - b.seq);
    if (!replay && !msg.isSelf) {
      this.lastIncomingAt = this.clock(); this.preSleepWaitSatisfiedAt = 0; this.preSleepWaitObservedAt = 0;
    }
    return msg;
  }
  readThrough(additional = []) {
    const seen = new Set([...this.modelSeenSeqs, ...additional.map(m => m.seq)]);
    let seq = this.lastReadThroughSeq;
    while (seq < this.lastUnreadSeq && seen.has(seq + 1)) seq++;
    return seq;
  }
  note(messages) { for (const m of messages) if (Number.isSafeInteger(m.seq)) this.modelSeenSeqs.add(m.seq); return this.readThrough(); }
  unreadPage(limit = 30, afterSeq) {
    limit = integer(limit, 1, 100, 30);
    const messages = afterSeq === undefined ? this.unread.slice(-limit) : this.unread.filter(m => m.seq > integer(afterSeq, 0, Number.MAX_SAFE_INTEGER)).slice(0, limit);
    return { ok: true, key: this.key, messages: messages.map(compactModelMessage), unreadCount: this.unread.length, readThroughSeq: this.note(messages) };
  }
  wakeSnapshot() {
    const packet = { messages: [], recent: [], unreadCount: this.unread.length, includedUnreadCount: 0, readThroughSeq: this.readThrough(), partial: this.unread.length > 0 };
    const add = (field, message) => {
      const previous = packet.readThroughSeq; packet[field].push(compactModelMessage(message));
      packet.includedUnreadCount = packet.messages.length;
      packet.readThroughSeq = this.readThrough([...packet.messages, ...packet.recent]);
      packet.partial = packet.messages.length < this.unread.length;
      if (JSON.stringify(packet).length <= 6000) return true;
      packet[field].pop(); packet.includedUnreadCount = packet.messages.length; packet.readThroughSeq = previous;
      packet.partial = packet.messages.length < this.unread.length; return false;
    };
    for (const msg of this.unread.slice(0, 12)) if (!add('messages', msg)) break;
    const unread = new Set(this.unread.map(m => m.seq));
    for (const msg of this.recentMessages.filter(m => !unread.has(m.seq)).slice(-4).reverse()) if (!add('recent', msg)) break;
    packet.recent.reverse(); return packet;
  }
  callSnapshot() {
    const messages=[];
    for(const msg of this.recentMessages.filter(m=>!m.isConfiguration).slice(-20).reverse()) {
      const next=[compactModelMessage(msg),...messages];
      if(JSON.stringify(next).length>6000)break;
      messages.unshift(compactModelMessage(msg));
    }
    return {messages,recent:[],unreadCount:this.unread.length,readThroughSeq:this.readThrough(messages),partial:messages.length<this.unread.length};
  }
  acknowledge(through = this.readThrough()) {
    through = integer(through, 0, Number.MAX_SAFE_INTEGER);
    if (through > this.readThrough() || through < this.lastReadThroughSeq) throw new PolicyError('throughSeq 超过已查看的连续消息水位');
    const before = this.unread.length;
    this.unread = this.unread.filter(m => m.seq > through); this.lastReadThroughSeq = through;
    for (const seq of this.modelSeenSeqs) if (seq <= through) this.modelSeenSeqs.delete(seq);
    return before - this.unread.length;
  }
  lastIncoming() { return [...this.recentMessages].reverse().find(m => !m.isSelf); }
  // Memory reads aggregate the IGNG account of the people who spoke most recently.
  actorQqs(limit = 12) {
    if (this.key.startsWith('private:')) return [this.key.split(':')[1]];
    const result = [];
    for (const message of [...this.recentMessages].reverse()) {
      if (message.isSelf || !message.userId) continue;
      const qq = String(message.userId);
      if (!/^[1-9][0-9]{0,19}$/.test(qq) || result.includes(qq)) continue;
      result.push(qq);
      if (result.length >= limit) break;
    }
    return result;
  }
  preSleepBlocked() {
    if (EXPLICIT_END_RE.test(String(this.lastIncoming()?.plain || this.lastIncoming()?.text || ''))) return false;
    const wait = this.config.preSleepWaitMs ?? 300000;
    if (this.lastIncomingAt && this.clock() - this.lastIncomingAt >= wait) return false;
    if (this.preSleepWaitSatisfiedAt && this.lastIncomingAt <= this.preSleepWaitSatisfiedAt) return false;
    if (this.preSleepWaitObservedAt && this.lastIncomingAt <= this.preSleepWaitObservedAt) return false;
    return true;
  }
  ensureWakeable() {
    const wc = this.wakeConfig, t = wc.triggers;
    t.speakerIds = this.key.startsWith('private:') ? [] : normalizeSpeakerIds(t.speakerIds);
    if (!(wc.mode === 'active' || t.anyMessage || t.atMention || t.nameMention || t.question || t.poke || t.keywords?.length || t.speakerIds.length || t.probability > 0 || (!wc.infinite && Date.parse(wc.sleepUntil) > this.clock()))) this.wakeConfig = defaultWakeConfig();
  }
  setWake(patch, through) {
    const wc = structuredClone(this.wakeConfig);
    if (patch.mode !== undefined) { if (!['active','diving'].includes(patch.mode)) throw new PolicyError('mode 无效'); wc.mode = patch.mode; wc.triggers.anyMessage = patch.mode === 'active'; }
    if (patch.infinite !== undefined) wc.infinite = !!patch.infinite;
    if (patch.triggers) {
      for (const name of ['atMention','nameMention','question','poke','anyMessage']) if (patch.triggers[name] !== undefined) wc.triggers[name] = !!patch.triggers[name];
      if (patch.triggers.probability !== undefined) wc.triggers.probability = Math.max(0, Math.min(1, Number(patch.triggers.probability) || 0));
      if (patch.triggers.speakerIds) wc.triggers.speakerIds = normalizeSpeakerIds(patch.triggers.speakerIds);
      if (patch.triggers.keywords) wc.triggers.keywords = patch.triggers.keywords.map(String).filter(s => s.trim()).slice(0,20).map(s => s.slice(0,80));
    }
    if (!wc.infinite) {
      const time = patch.sleepUntil ? Date.parse(patch.sleepUntil) : this.clock() + Math.max(60000, Number(patch.sleepMs) || 300000);
      if (!Number.isFinite(time) || time <= this.clock()) throw new PolicyError('sleepUntil 必须在未来'); wc.sleepUntil = new Date(time).toISOString();
    } else wc.sleepUntil = null;
    if (wc.mode !== 'active' && !wc.triggers.anyMessage && this.preSleepBlocked()) throw new PolicyError('潜水前必须完成沉睡前观察；先 qq_wait_for_messages(timeoutMs=300000)');
    if (through !== undefined) this.acknowledge(through);
    this.lastActionAt=this.clock();
    wc.confirmedAt = this.clock(); wc.confirmedBy = 'agent'; this.wakeConfig = wc; this.ensureWakeable();
    return { ok: true, wakeConfig: this.wakeConfig, readThroughSeq: this.readThrough(), unreadCount: this.unread.length };
  }
  wakeReason(msg, random = Math.random) {
    if (msg.isSelf || msg.commandHandled) return null;
    if (this.key.startsWith('private:')) return 'private';
    if (msg.atBot || msg.replyToBot) return 'atMention'; // Product guarantee: direct requests bypass ordinary cost throttles.
    if (!this.chatMode || msg.observeOnly || msg.isConfiguration) return null;
    const t = this.wakeConfig.triggers, text = String(msg.plain || msg.text || '');
    if (this.wakeConfig.mode === 'active' || t.anyMessage) return 'anyMessage';
    if (t.nameMention && (this.config.nicknames || ['云萤','莹宝']).some(s => text.includes(s))) return 'nameMention';
    if (t.poke && msg.poke) return 'poke';
    if (t.speakerIds.includes(String(msg.userId))) return 'speaker';
    if ((t.keywords || []).some(k => /^[a-z0-9_]{1,4}$/i.test(k) ? new RegExp(`\\b${k}\\b`, 'i').test(text) : text.includes(k))) return 'keyword';
    if (t.question && /[?？]|(?:谁|什么|怎么|为何|为什么|吗|呢)(?:[。！!\s]*$)/.test(text)) return 'question';
    return t.probability > 0 && random() < t.probability ? 'probability' : null;
  }
  replyTiming() {
    const quietMs = this.config.minQuietMs ?? 10000;
    return { purpose: 'reply', quietMs, remainingQuietMs: Math.max(0, quietMs - Math.max(0, this.clock() - this.lastIncomingAt)) };
  }
  sent(messageId, text) {
    this.lastAiReplyAt = this.clock(); this.lastActionAt = this.clock(); this.sendTimes.push(this.clock());
    this.sendTimes = this.sendTimes.filter(t => this.clock() - t < 3600000);
    this.preSleepWaitSatisfiedAt = 0; this.preSleepWaitObservedAt = 0;
    this.recentMessages.push({ messageId, text, isSelf: true, time: this.clock(), sender: '云萤' });
    this.recentMessages = this.recentMessages.slice(-100);
  }
  async wait(args, signal, pause = async ms => sleep(ms, undefined, { signal })) {
    if (this.waiting) throw new PolicyError('同一会话已有等待调用');
    this.waiting = true;
    try {
      const reply = args.purpose === 'reply';
      const minimum = this.config.minQuietMs ?? 10000;
      const maxQuiet = Math.max(minimum, 120000);
      const timeout = Math.min(600000, Math.max(this.config.waitMinMs ?? 5000, Number(args.timeoutMs) || 30000));
      const quietMs = Math.min(maxQuiet, Math.max(minimum, Number(args.quietMs) || minimum));
      const minNew = Math.max(1, Math.min(100, Number(args.minNewMessages) || 1));
      const baseline = reply ? this.readThrough() : this.lastUnreadSeq;
      const start = this.clock(); let lastNewAt = reply ? (this.lastIncomingAt || start) : 0;
      let observed = this.lastUnreadSeq, arrived = false;
      if (reply) {
        while (this.clock() - start < timeout) {
          signal?.throwIfAborted(); if (observed !== this.lastUnreadSeq) { observed = this.lastUnreadSeq; lastNewAt = this.lastIncomingAt; }
          const remaining = quietMs - (this.clock() - lastNewAt); if (remaining <= 0) break;
          await pause(Math.max(1, Math.min(100, remaining, timeout - (this.clock() - start))));
        }
        lastNewAt = this.lastIncomingAt || start; arrived = this.lastUnreadSeq > baseline;
      } else while (this.clock() - start < timeout) {
        signal?.throwIfAborted();
        if (this.lastUnreadSeq - baseline >= minNew) {
          arrived = true; observed = this.lastUnreadSeq; lastNewAt = this.clock();
          while (this.clock() - lastNewAt < quietMs && this.clock() - start < timeout + maxQuiet + 5000) {
            signal?.throwIfAborted();
            if (this.lastUnreadSeq > observed) { observed = this.lastUnreadSeq; lastNewAt = this.clock(); }
            await pause(Math.min(200, quietMs - (this.clock() - lastNewAt)));
          }
          break;
        }
        await pause(Math.min(300, timeout - (this.clock() - start)));
      }
      signal?.throwIfAborted();
      const waitedMs = this.clock() - start, quiet = (reply || arrived) && this.clock() - lastNewAt >= quietMs;
      const preSleepWaitMs = this.config.preSleepWaitMs ?? 300000;
      const satisfied = !reply && (!arrived ? waitedMs >= preSleepWaitMs : quiet && this.clock() - lastNewAt >= preSleepWaitMs);
      if (satisfied) { this.preSleepWaitSatisfiedAt = this.clock(); this.preSleepWaitObservedAt = 0; }
      else if (!reply && arrived && timeout >= preSleepWaitMs) this.preSleepWaitObservedAt = this.clock();
      const history = [...this.unread, ...this.recentMessages];
      const pending = [...new Map(history.filter(m => m.seq > baseline && !m.isSelf).map(m => [m.seq,m])).values()].sort((a,b) => a.seq-b.seq);
      const newMessages = reply || arrived ? pending.slice(0,100) : [];
      return { ok: true, key: this.key, purpose: reply ? 'reply' : 'messages', arrived, quiet, quietMs,
        suggestedQuietMs: minimum, speakerLikelyDone: quiet, lastMessageUnfinished: looksLikeUnfinished(String((newMessages.at(-1) || this.lastIncoming())?.plain || (newMessages.at(-1) || this.lastIncoming())?.text || '')),
        timeout: reply ? !quiet : !arrived || !quiet, waitedMs, preSleepWaitSatisfied: satisfied,
        preSleepWaitObserved: !reply && !!this.preSleepWaitObservedAt,
        ...(!reply ? { preSleepWaitMs, preSleepWaitRemainingMs: this.preSleepBlocked() ? Math.max(0, preSleepWaitMs - (this.clock()-this.lastIncomingAt)) : 0 } : {}),
        newMessages: newMessages.map(compactModelMessage), readThroughSeq: this.note(newMessages), unreadCount: this.unread.length,
        partial: pending.length > newMessages.length };
    } finally { this.waiting = false; }
  }
}
