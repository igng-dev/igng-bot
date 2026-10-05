"""Durable infrastructure inbox and outgoing tool-call ledger (not an Agent loop)."""
import hashlib
import json
import uuid
from .settings import conversation_key


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def ingress_identity(raw):
    kind = "group" if raw.get("group_id") else "private"
    identity = raw.get("group_id") if kind == "group" else raw.get("user_id")
    if kind == "private" and raw.get("post_type") == "message_sent":
        identity = raw.get("target_id") or raw.get("peer_id") or identity
    key = conversation_key(kind, identity)
    event_type = str(raw.get("notice_type") or "message")
    if event_type == "notify":
        event_type += ":" + str(raw.get("sub_type") or "")
    mid = raw.get("message_id")
    if mid in (None, "") and event_type == "message":
        raise ValueError("message event without message_id")
    # Notices without stable IDs use a bounded event fingerprint; raw history remains intact.
    natural = str(mid) if mid not in (None, "") else hashlib.sha256(encode(raw).encode()).hexdigest()
    return key, str(uuid.uuid5(uuid.NAMESPACE_URL, f"qq:{key}:{event_type}:{natural}"))


class Journal:
    def __init__(self, conn):
        self.conn = conn

    def enqueue(self, raw):
        key, event_id = ingress_identity(raw)
        with self.conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO yunying_ingress (event_id,conversation_key,raw_event) VALUES (%s,%s,%s)",
                        (event_id, key, encode(raw)))
        return key, event_id

    def pending(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT i.* FROM yunying_ingress i WHERE i.delivery_status='pending' AND i.recording_status='recorded'
                AND i.available_at<=UTC_TIMESTAMP(6) AND NOT EXISTS (
                  SELECT 1 FROM yunying_ingress earlier WHERE earlier.conversation_key=i.conversation_key
                    AND earlier.delivery_status='pending' AND earlier.id<i.id)
                ORDER BY i.id LIMIT 1""")
            return cur.fetchone()

    def pending_recording(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT i.* FROM yunying_ingress i WHERE i.recording_status='pending' AND i.delivery_status='pending'
                AND i.recording_available_at<=UTC_TIMESTAMP(6) AND NOT EXISTS (
                  SELECT 1 FROM yunying_ingress earlier WHERE earlier.conversation_key=i.conversation_key
                    AND earlier.recording_status='pending' AND earlier.id<i.id)
                ORDER BY i.id LIMIT 1""")
            return cur.fetchone()

    def recording_retry(self, event_id, attempts, error):
        delay = min(60, 2 ** min(6, attempts))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_ingress SET recording_attempts=recording_attempts+1,recording_error=%s,recording_available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND) WHERE event_id=%s",
                        (type(error).__name__, delay, event_id))

    def group_control(self, event_id, group_id, field, desired=None):
        """Apply a mechanical control once even after a crash before its acknowledgement."""
        if field not in {"is_chat_mode", "social_paused"}:
            raise ValueError("invalid group control")
        self.conn.begin()
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT command_result FROM yunying_ingress WHERE event_id=%s FOR UPDATE", (event_id,))
                prior = cur.fetchone()
                if not prior:
                    raise ValueError("control event is not durable")
                if prior["command_result"]:
                    result = decode(prior["command_result"])
                else:
                    cur.execute("INSERT IGNORE INTO group_configs (group_id) VALUES (%s)", (group_id,))
                    cur.execute(f"SELECT {field} FROM group_configs WHERE group_id=%s FOR UPDATE", (group_id,))
                    current = bool(cur.fetchone()[field])
                    result = {field: not current if desired is None else bool(desired)}
                    cur.execute(f"UPDATE group_configs SET {field}=%s WHERE group_id=%s", (int(result[field]), group_id))
                    cur.execute("UPDATE yunying_ingress SET command_result=%s WHERE event_id=%s", (encode(result), event_id))
            self.conn.commit()
            return result[field]
        except Exception:
            self.conn.rollback()
            raise

    def prepare(self, event_id, payload):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_ingress SET prepared_event=%s,recording_status='recorded',recorded_at=UTC_TIMESTAMP(6),recording_error=NULL WHERE event_id=%s", (encode(payload) if payload is not None else None, event_id))

    def finish(self, event_id):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_ingress SET delivery_status='delivered',delivered_at=UTC_TIMESTAMP(6),last_error=NULL WHERE event_id=%s", (event_id,))

    def retry(self, event_id, attempts, error):
        # Persist only the error class; URLs, request payloads and credentials stay out of error logs.
        delay = min(60, 2 ** min(6, attempts))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_ingress SET attempts=attempts+1,last_error=%s,available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND) WHERE event_id=%s", (type(error).__name__, delay, event_id))

    def begin_send(self, request_id, key, payload):
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        with self.conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO yunying_sends (request_id,conversation_key,payload_hash,payload) VALUES (%s,%s,%s,%s)", (request_id, key, digest, encode(payload)))
            cur.execute("SELECT * FROM yunying_sends WHERE request_id=%s", (request_id,))
            row = cur.fetchone()
            if row["conversation_key"] != key or row["payload_hash"] != digest:
                raise ValueError("send request identity conflict")
            if row["status"] != "pending":
                return False, decode(row["result"]) or {"ok": False, "status": "unknown", "error": "发送结果未确认；不得自动重发"}
            cur.execute("UPDATE yunying_sends SET status='sending' WHERE request_id=%s AND status='pending'", (request_id,))
            return cur.rowcount == 1, None

    def finish_send(self, request_id, status, result):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_sends SET status=%s,message_id=%s,result=%s WHERE request_id=%s", (status, result.get("message_id"), encode(result), request_id))
