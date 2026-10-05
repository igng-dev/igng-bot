import mysql from 'mysql2/promise';
import { randomUUID, createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { PolicyError, canonicalKey, bounded, integer } from './policy.js';
const enabled = value => /^(1|true|yes|on)$/i.test(String(value ?? '').trim());
// Aliyun RDS enforces require_secure_transport. DB_SSL encrypts the connection;
// DB_SSL_CA verifies the server certificate, otherwise TLS is unverified.
export function dbSslOptions(env = process.env) {
  if (!enabled(env.DB_SSL)) return null;
  const ca = String(env.DB_SSL_CA || '').trim();
  if (ca) return { ca: readFileSync(ca), rejectUnauthorized: true };
  return { rejectUnauthorized: enabled(env.DB_SSL_VERIFY) };
}
const json = value => typeof value === 'string' ? JSON.parse(value) : value;
const hash = value => createHash('sha256').update(value).digest('hex');
const identityId = qq => {
  const bytes=createHash('sha1').update(Buffer.from('6ba7b8119dad11d180b400c04fd430c8','hex')).update(`identity:qq:${qq}`).digest().subarray(0,16);
  bytes[6]=(bytes[6]&15)|80;bytes[8]=(bytes[8]&63)|128;
  const hex=bytes.toString('hex');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
};
const visible = (actor,doc) => {
  // Person memory is cross-group; never leak the conversation that sourced it.
  if(actor.admin||doc.document_type!=='person')return doc;
  const {scope_key,...result}=doc;return {...result,scope:'person'};
};
const signedId = key => key.startsWith('group:') ? key.split(':')[1] : '-' + key.split(':')[1];
export class MySQLStore {
  constructor(pool, lockConnection) { this.pool = pool; this.lockConnection = lockConnection; this.healthy = true; this.ownerResolver = null; }
  static async open(env = process.env) {
    const ssl = dbSslOptions(env);
    const opts = { host: env.DB_HOST || '127.0.0.1', port: Number(env.DB_PORT || 3306), user: env.DB_USER,
      password: env.DB_PASSWORD, database: env.DB_NAME || 'igng_bot', charset: 'utf8mb4',
      timezone: 'Z', dateStrings:true, supportBigNumbers: true, bigNumberStrings: true, connectionLimit: 6,
      ...(ssl ? { ssl } : {}) };
    const pool = mysql.createPool(opts);
    pool.pool.on('connection', connection=>connection.query("SET time_zone = '+00:00'"));
    const lock = await mysql.createConnection(opts);
    await lock.query("SET time_zone = '+00:00'");
    const store = new MySQLStore(pool, lock);
    try {
      await pool.query('SELECT version FROM yunying_schema_migrations ORDER BY version');
      const [rows] = await lock.query("SELECT GET_LOCK(CONCAT('yunying-dsh:', DATABASE()),0) acquired");
      if (Number(rows[0].acquired) !== 1) throw new Error('another YunYing profile owns this database');
      lock.on('error', () => { store.healthy = false; store.onOwnershipLost?.(); });
      return store;
    } catch (error) { await pool.end(); await lock.end(); throw error; }
  }
  async heartbeat() {
    try { const [rows] = await this.lockConnection.query({sql:"SELECT IS_USED_LOCK(CONCAT('yunying-dsh:',DATABASE())) = CONNECTION_ID() owned",timeout:5000});
      if (Number(rows[0].owned) !== 1) throw new Error('social runtime ownership lost');
    } catch (error) { this.healthy = false; this.onOwnershipLost?.(); throw error; }
  }
  async close() { this.healthy = false; await this.lockConnection?.end(); await this.pool.end(); }
  async query(sql, values = []) { if (!this.healthy) throw new PolicyError('运行实例已失去独占权限'); return (await this.pool.execute(sql, values))[0]; }
  async transaction(work) {
    if (!this.healthy) throw new PolicyError('运行实例已失去独占权限');
    const conn = await this.pool.getConnection();
    try { await conn.beginTransaction(); const result = await work(conn); await conn.commit(); return result; }
    catch (error) { await conn.rollback(); throw error; } finally { conn.release(); }
  }
  async mapping(key) {
    canonicalKey(key); const [kind, id] = key.split(':');
    await this.query(`INSERT IGNORE INTO yunying_sessions (conversation_key,conversation_type,external_id,dsh_session_id) VALUES (?,?,?,?)`, [key, kind, id, randomUUID()]);
    const rows = await this.query('SELECT * FROM yunying_sessions WHERE conversation_key=?', [key]);
    return { ...rows[0], social_state: json(rows[0].social_state) };
  }
  async mappings() { return this.query('SELECT * FROM yunying_sessions ORDER BY created_at'); }
  async policy(key) {
    // Private conversations always participate (the infrastructure gates them by tier);
    // groups follow the single chat-mode switch.
    if(key.startsWith('private:')) return {chatMode:true};
    const [row]=await this.query('SELECT is_chat_mode FROM group_configs WHERE group_id=?',[key.split(':')[1]]);
    return {chatMode:!!row?.is_chat_mode};
  }
  async beginDirect(key,eventId) {
    const [event]=await this.query('SELECT payload FROM yunying_events WHERE event_id=? AND conversation_key=?',[eventId,key]);
    const message=event&&json(event.payload);
    if(!message||message.isSelf||message.commandHandled||!(message.atBot||message.replyToBot))throw new PolicyError('缺少真实呼叫来源');
    await this.query('UPDATE yunying_sessions SET direct_event_id=?,direct_expires_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 10 MINUTE) WHERE conversation_key=?',[eventId,key]);
  }
  async endDirect(key,eventId) {
    await this.query('UPDATE yunying_sessions SET direct_event_id=NULL,direct_expires_at=NULL WHERE conversation_key=? AND direct_event_id=?',[key,eventId]);
  }
  async ready(key) { await this.query("UPDATE yunying_sessions SET provisioning_status='ready' WHERE conversation_key=?", [key]); }
  async saveState(state) { await this.query('UPDATE yunying_sessions SET social_state=? WHERE conversation_key=?', [JSON.stringify(state.snapshot()), state.key]); }
  async accept(payload) {
    bounded(payload.eventId, 96); canonicalKey(payload.key);
    return this.transaction(async conn => {
      const [found] = await conn.execute('SELECT * FROM yunying_events WHERE event_id=?', [payload.eventId]);
      if (found.length) {
        if (found[0].conversation_key !== payload.key) throw new PolicyError('事件标识冲突');
        return { ...found[0], seq: Number(found[0].seq), payload: json(found[0].payload), dsh_message: json(found[0].dsh_message) };
      }
      const [maps] = await conn.execute('SELECT next_seq FROM yunying_sessions WHERE conversation_key=? FOR UPDATE', [payload.key]);
      const seq = Number(maps[0].next_seq);
      if (!Number.isSafeInteger(seq)) throw new Error('social sequence exhausted');
      await conn.execute('INSERT INTO yunying_events (event_id,conversation_key,seq,event_type,message_id,payload) VALUES (?,?,?,?,?,?)', [payload.eventId, payload.key, seq, payload.kind, payload.messageId || null, JSON.stringify(payload)]);
      await conn.execute('UPDATE yunying_sessions SET next_seq=next_seq+1 WHERE conversation_key=?', [payload.key]);
      return { event_id: payload.eventId, conversation_key: payload.key, seq, delivered: 0, payload, dsh_message: null };
    });
  }
  async events(key, after = 0) {
    const rows = await this.query('SELECT * FROM yunying_events WHERE conversation_key=? AND seq>? ORDER BY seq', [key, after]);
    return rows.map(r => ({ ...r, seq: Number(r.seq), payload: json(r.payload), dsh_message: json(r.dsh_message) }));
  }
  async bindMessage(eventId, message) { await this.query('UPDATE yunying_events SET dsh_message=? WHERE event_id=? AND dsh_message IS NULL', [JSON.stringify(message), eventId]); }
  async delivered(eventId) { await this.query('UPDATE yunying_events SET delivered=1 WHERE event_id=?', [eventId]); }
  async identity(qq, displayName = '') {
    if (!/^[1-9][0-9]{0,19}$/.test(String(qq))) throw new PolicyError('身份格式无效');
    return this.transaction(async conn => {
      const id = identityId(String(qq));
      await conn.execute('INSERT IGNORE INTO memory_identities (id,display_name) VALUES (?,?)', [id, displayName.slice(0,160)]);
      const [created]=await conn.execute("INSERT IGNORE INTO memory_identity_bindings (provider,external_id,identity_id,verified_by) VALUES ('qq',?,?,'onebot')",[String(qq),id]);
      const [bindings]=await conn.execute("SELECT * FROM memory_identity_bindings WHERE provider='qq' AND external_id=?",[String(qq)]);
      if(created.affectedRows)await conn.execute("INSERT INTO memory_identity_audit (provider,external_id,identity_id,operation,actor,detail) VALUES ('qq',?,?,'bind','onebot','{}')",[String(qq),bindings[0].identity_id]);
      return bindings[0];
    });
  }
  async access(actor, prefix = 'd') {
    // Visibility is enforced by SQL before matching, counting or producing snippets.
    // Group documents stay scoped to their conversation; person documents are keyed by
    // QQ and readable across groups by every QQ of the same IGNG account.
    if (actor.admin === true) return { sql: '1=1', values: [] };
    let qqs = Array.isArray(actor.qqs) && actor.qqs.length ? actor.qqs : (actor.qq ? [actor.qq] : []);
    qqs = [...new Set(qqs.map(String).filter(q => /^[1-9][0-9]{0,19}$/.test(q)))].slice(0, 50);
    if (qqs.length && typeof this.ownerResolver === 'function') {
      try {
        const expanded = await this.ownerResolver(qqs);
        if (Array.isArray(expanded) && expanded.length) {
          qqs = [...new Set(expanded.map(String).filter(q => /^[1-9][0-9]{0,19}$/.test(q)))].slice(0, 500);
        }
      } catch { /* fail closed: keep only the literal QQs on resolver failure */ }
    }
    const group = `(${prefix}.document_type='group' AND ${prefix}.scope_key=?)`;
    if (!qqs.length) return { sql: group, values: [actor.key] };
    const placeholders = qqs.map(() => '?').join(',');
    return { sql: `(${group} OR ${prefix}.person_qq IN (${placeholders}))`, values: [actor.key, ...qqs] };
  }
  async audit(actor, operation, doc, permitted, detail = {}) {
    await this.query('INSERT INTO memory_audit (document_id,version,operation,actor,scope_key,allowed,detail) VALUES (?,?,?,?,?,?,?)', [doc?.id || null, doc?.current_version || null, operation, actor.admin ? 'owner-api' : actor.sessionId, actor.key || 'owner', Number(permitted), JSON.stringify(detail)]);
  }
  async memorySearch(actor, args) {
    const query = bounded(args.query, 200, false); const limit = integer(args.limit, 1, 30, 10);
    const filter = await this.access(actor); const match = '%' + query.replace(/[\\%_]/g, s => '\\' + s) + '%';
    const rows = await this.query(`SELECT d.id,d.title,d.document_type,d.scope_key,d.visibility,d.identity_id,d.current_version,
      LEFT(d.markdown,800) snippet FROM memory_documents d WHERE ${filter.sql} AND d.status='active'
      AND (d.title LIKE ? OR d.markdown LIKE ?) ORDER BY d.updated_at DESC LIMIT ?`, [...filter.values, match, match, limit]);
    await this.audit(actor, 'search', null, true, { count: rows.length });
    return { ok: true, documents: rows.map(doc=>visible(actor,doc)) };
  }
  async memoryRead(actor, id, includeForgotten = false) {
    bounded(id, 36); const filter = await this.access(actor);
    const rows = await this.query(`SELECT d.* FROM memory_documents d WHERE d.id=? AND ${filter.sql} ${includeForgotten && actor.admin ? '' : "AND d.status='active'"}`, [id, ...filter.values]);
    if (!rows.length) { await this.audit(actor, 'read', null, false); throw new PolicyError('记忆不可用'); }
    await this.audit(actor, 'read', rows[0], true);
    // Person memory sources stay the person's own statements; the source conversation is never returned.
    return { ok: true, document: visible(actor,rows[0]) };
  }
  async sourceRows(actor, sources, person = null) {
    if (!Array.isArray(sources) || !sources.length || sources.length > 12) throw new PolicyError('写入必须提供 1 到 12 条当前会话消息来源');
    const ids = [...new Set(sources.map(s => bounded(s, 96)))];
    const rows = await this.query(`SELECT e.*,m.is_recalled FROM yunying_events e
      LEFT JOIN message_logs m ON m.group_id=? AND m.msg_id=e.message_id
      WHERE e.conversation_key=? AND e.event_id IN (${ids.map(() => '?').join(',')})`, [signedId(actor.key), actor.key, ...ids]);
    if (rows.length !== ids.length) throw new PolicyError('记忆来源不可用');
    for (const row of rows) {
      row.payload = json(row.payload);
      if (row.event_type !== 'message' || row.is_recalled || row.payload.isSelf || row.payload.commandHandled || !actor.seenSeqs.has(Number(row.seq))) throw new PolicyError('只能引用本轮已查看且未撤回的真实来信');
      if (person && row.payload.userId !== person.external_id) throw new PolicyError('个人记忆只能引用本人陈述');
    }
    return rows;
  }
  async revision(conn, actor, doc, sources, operation, reason) {
    await conn.execute('INSERT INTO memory_versions (document_id,version,title,markdown,status,content_hash,operation,reason,actor,dsh_session_id) VALUES (?,?,?,?,?,?,?,?,?,?)', [doc.id, doc.current_version, doc.title, doc.markdown, doc.status, hash(doc.markdown), operation, reason, actor.admin ? 'owner-api' : actor.sessionId, actor.sessionId || null]);
    for (const source of sources) await conn.execute('INSERT INTO memory_sources (document_id,version,event_id,conversation_key,message_id,identity_id) VALUES (?,?,?,?,?,?)', [doc.id, doc.current_version, source.event_id, source.conversation_key, source.message_id, doc.identity_id]);
    await conn.execute('INSERT INTO memory_audit (document_id,version,operation,actor,scope_key,allowed,detail) VALUES (?,?,?,?,?,1,?)', [doc.id, doc.current_version, operation, actor.admin ? 'owner-api' : actor.sessionId, actor.key || 'owner', JSON.stringify({ reason })]);
  }
  async memoryWrite(actor, args) {
    // Person memory is keyed by the subject QQ and readable across groups by the whole
    // IGNG account; group memory is model-written and stays scoped to this conversation.
    let title = bounded(args.title, 240), markdown = bounded(args.markdown, 24000), person = null;
    if (args.personQQ) person = await this.identity(String(args.personQQ));
    const sources = await this.sourceRows(actor, args.sources, person);
    if (person && !sources.some(s => s.payload.userId === person.external_id)) throw new PolicyError('个人记忆必须有本人来源');
    if (person) {
      // Cross-group person memory quotes only this person's own current-conversation
      // statements; the model cannot launder private prose into another group.
      title = `Person QQ ${person.external_id}`;
      markdown = sources.map(s => `> ${String(s.payload.plain || s.payload.text).replace(/\n/g, '\n> ')}`).join('\n\n');
    }
    const doc = { id: randomUUID(), title, markdown, document_type: person ? 'person' : 'group', scope_key: actor.key,
      visibility: person ? 'shared_person' : 'scope_private', identity_id: person?.identity_id || null, person_qq: person?.external_id || null,
      current_version: 1, status: 'active' };
    await this.transaction(async conn => {
      await conn.execute('INSERT INTO memory_documents (id,title,markdown,document_type,scope_key,visibility,identity_id,person_qq,current_version,status) VALUES (?,?,?,?,?,?,?,?,?,?)',
        [doc.id, doc.title, doc.markdown, doc.document_type, doc.scope_key, doc.visibility, doc.identity_id, doc.person_qq, doc.current_version, doc.status]);
      await this.revision(conn, actor, doc, sources, 'write', bounded(args.reason || 'explicit memory', 500));
    });
    return { ok: true, document: visible(actor,doc) };
  }
  async memoryUpdate(actor, args, forget = false) {
    const expected = integer(args.expectedVersion, 1, 1e9); const filter = await this.access(actor);
    const old = (await this.memoryRead(actor, args.id)).document;
    let sources = [];
    if (!forget) {
      let person = null;
      if (old.document_type === 'person') {
        if (old.person_qq) person = { external_id: String(old.person_qq), identity_id: old.identity_id };
        else {
          const [binding] = await this.query("SELECT external_id,identity_id FROM memory_identity_bindings WHERE identity_id=? AND provider='qq' LIMIT 1", [old.identity_id]);
          if (binding) person = binding;
        }
        if (!person) throw new PolicyError('个人记忆缺少身份绑定');
      }
      sources = await this.sourceRows(actor, args.sources, person);
      if (person) {
        args = { ...args, title: old.title, markdown: sources.map(s => `> ${String(s.payload.plain || s.payload.text).replace(/\n/g, '\n> ')}`).join('\n\n') };
      }
    } else if (!actor.admin) {
      sources = await this.sourceRows(actor, args.sources);
      // Forgetting cross-group person memory requires a request by that same person.
      if (old.document_type === 'person') {
        const bindings = await this.query("SELECT external_id FROM memory_identity_bindings WHERE identity_id=? AND provider='qq'", [old.identity_id]);
        const owners = new Set([...bindings.map(b => String(b.external_id)), ...(old.person_qq ? [String(old.person_qq)] : [])]);
        if (!sources.some(s => owners.has(String(s.payload.userId)))) throw new PolicyError('删除个人记忆需要本人来源');
      }
    }
    return this.transaction(async conn => {
      const [rows] = await conn.execute(`SELECT d.* FROM memory_documents d WHERE d.id=? AND ${filter.sql} FOR UPDATE`, [args.id, ...filter.values]);
      const doc = rows[0];
      if (!doc || Number(doc.current_version) !== expected || doc.status !== 'active') throw new PolicyError('版本冲突；请重新读取记忆');
      doc.current_version = expected + 1;
      if (forget) { doc.status = 'forgotten'; doc.markdown = ''; }
      else { doc.markdown = bounded(args.markdown, 24000); if (args.title !== undefined) doc.title = bounded(args.title, 240); }
      await conn.execute('UPDATE memory_documents SET title=?,markdown=?,current_version=?,status=? WHERE id=?', [doc.title, doc.markdown, doc.current_version, doc.status, doc.id]);
      await this.revision(conn, actor, doc, sources, forget ? 'forget' : 'update', bounded(args.reason, 500));
      return { ok: true, document: visible(actor,doc) };
    });
  }
  async adminVersions(id) { return this.query('SELECT * FROM memory_versions WHERE document_id=? ORDER BY version DESC', [bounded(id, 36)]); }
  async adminRollback(id, version, expected, reason) {
    bounded(reason, 500);
    return this.transaction(async conn => {
      const [rows] = await conn.execute('SELECT * FROM memory_documents WHERE id=? FOR UPDATE', [bounded(id, 36)]);
      const [versions] = await conn.execute('SELECT * FROM memory_versions WHERE document_id=? AND version=?', [id, integer(version, 1, 1e9)]);
      const doc = rows[0], old = versions[0];
      if (!doc || !old || Number(doc.current_version) !== integer(expected, 1, 1e9)) throw new PolicyError('版本冲突或版本不存在');
      Object.assign(doc, { title: old.title, markdown: old.markdown, status: old.status, current_version: Number(doc.current_version) + 1 });
      await conn.execute('UPDATE memory_documents SET title=?,markdown=?,status=?,current_version=? WHERE id=?', [doc.title, doc.markdown, doc.status, doc.current_version, id]);
      await this.revision(conn, { admin: true }, doc, [], 'rollback', reason);
      return { ok: true, document: doc };
    });
  }
  async recordCall(recordId, sessionId, seq, payload) {
    await this.query('INSERT IGNORE INTO yunying_ai_records (record_id,dsh_session_id,request_seq,payload) VALUES (?,?,?,?)', [recordId, sessionId, seq, JSON.stringify(payload)]);
  }
}
