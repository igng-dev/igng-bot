"""Durable infrastructure inbox and outgoing tool-call ledger (not an Agent loop)."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from .settings import conversation_key


_SAFE_LATENCY_ATTRIBUTE_KEYS = {
    "postType", "turn", "step", "attemptNo", "provider", "model", "requestModel",
    "success", "ttftMs", "firstOutputAt", "toolName", "callId", "isError",
    "httpStatus", "retcode", "messageSent", "sendStatus", "reason", "spanKind",
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _event_time(raw):
    try:
        value = float(raw.get("time"))
        # OneBot timestamps are seconds.  Reject obviously invalid values so a
        # malformed event cannot create a multi-year latency sample.
        if value <= 0 or value > 4102444800:
            raise ValueError
        return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return _now()


def _safe_latency_attributes(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key not in _SAFE_LATENCY_ATTRIBUTE_KEYS:
            continue
        if isinstance(item, bool) or item is None or isinstance(item, (int, float)):
            result[key] = item
        elif isinstance(item, str):
            result[key] = item[:191]
    return result if len(encode(result)) <= 4096 else {}


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
        echo_message_id = str(raw.get("message_id")) if raw.get("post_type") == "message_sent" and raw.get("message_id") not in (None, "") else None
        with self.conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO yunying_ingress (event_id,echo_message_id,conversation_key,raw_event) VALUES (%s,%s,%s,%s)",
                        (event_id, echo_message_id, key, encode(raw)))
            cur.execute("SELECT created_at FROM yunying_ingress WHERE event_id=%s", (event_id,))
            created_at = cur.fetchone()["created_at"]
        self.record_latency_span({
            "span_id": f"ingress:{event_id}", "correlation_id": event_id,
            "span_type": "ingress", "started_at": _event_time(raw),
            "ended_at": created_at, "status": "received",
            "attributes": {"postType": str(raw.get("post_type") or "")[:32]},
        })
        if raw.get("post_type") == "message_sent":
            self.record_echo_latency(raw.get("message_id"), event_id, _event_time(raw))
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
        if field not in {"is_chat_mode"}:
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
            cur.execute("SELECT created_at,recorded_at FROM yunying_ingress WHERE event_id=%s", (event_id,))
            row = cur.fetchone()
        if row:
            self.record_latency_span({
                "span_id": f"preprocess:{event_id}", "correlation_id": event_id,
                "span_type": "preprocess", "started_at": row["created_at"],
                "ended_at": row["recorded_at"], "status": "recorded" if payload is not None else "skipped",
            })

    def finish(self, event_id):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_ingress SET delivery_status='delivered',delivered_at=UTC_TIMESTAMP(6),last_error=NULL WHERE event_id=%s", (event_id,))
            cur.execute("SELECT recorded_at,delivered_at FROM yunying_ingress WHERE event_id=%s", (event_id,))
            row = cur.fetchone()
        if row:
            self.record_latency_span({
                "span_id": f"dsh-delivery:{event_id}", "correlation_id": event_id,
                "span_type": "dsh_delivery", "started_at": row["recorded_at"] or row["delivered_at"],
                "ended_at": row["delivered_at"], "status": "accepted",
            })

    def retry(self, event_id, attempts, error):
        # Persist only the error class; URLs, request payloads and credentials stay out of error logs.
        delay = min(60, 2 ** min(6, attempts))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_ingress SET attempts=attempts+1,last_error=%s,available_at=DATE_ADD(UTC_TIMESTAMP(6),INTERVAL %s SECOND) WHERE event_id=%s", (type(error).__name__, delay, event_id))

    def begin_send(self, request_id, key, payload):
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        trace_id = payload.get("_trace_id") if isinstance(payload, dict) else None
        task_key = payload.get("_task_key") if isinstance(payload, dict) else None
        with self.conn.cursor() as cur:
            cur.execute("INSERT IGNORE INTO yunying_sends (request_id,conversation_key,payload_hash,payload,trace_id,task_key) VALUES (%s,%s,%s,%s,%s,%s)",
                        (request_id, key, digest, encode(payload), trace_id, task_key))
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
            cur.execute("""UPDATE yunying_sends
                SET status=%s,message_id=%s,result=%s,
                    send_confirmed_at=CASE WHEN %s IN ('sent','failed')
                      THEN COALESCE(send_confirmed_at,UTC_TIMESTAMP(6)) ELSE send_confirmed_at END
                WHERE request_id=%s""",
                        (status, result.get("message_id"), encode(result), status, request_id))
            cur.execute("""SELECT i.event_id,i.raw_event FROM yunying_ingress i
                WHERE i.echo_message_id=%s
                ORDER BY i.id LIMIT 1""", (str(result.get("message_id") or ""),))
            echo = cur.fetchone()
        if echo:
            raw = decode(echo["raw_event"])
            self.record_echo_latency(result.get("message_id"), echo["event_id"], _event_time(raw))

    def record_latency_span(self, span):
        """Upsert one bounded span into the local durable mirror outbox.

        The site mirror is deliberately downstream of this table.  A site
        outage therefore cannot make the OneBot/DSH durability path lose a
        timing sample or retry a QQ action.
        """
        span_id = str(span.get("span_id") or "")[:160]
        span_type = str(span.get("span_type") or "")[:64]
        if not span_id or not span_type or span.get("started_at") is None:
            return
        attrs = _safe_latency_attributes(span.get("attributes"))
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO yunying_latency_spans
                (span_id,trace_id,task_key,correlation_id,span_type,parent_span_id,started_at,ended_at,status,attributes,mirror_status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
                ON DUPLICATE KEY UPDATE
                  trace_id=COALESCE(VALUES(trace_id),trace_id),
                  task_key=COALESCE(VALUES(task_key),task_key),
                  correlation_id=COALESCE(VALUES(correlation_id),correlation_id),
                  span_type=VALUES(span_type), parent_span_id=COALESCE(VALUES(parent_span_id),parent_span_id),
                  started_at=LEAST(started_at,VALUES(started_at)),
                  ended_at=COALESCE(VALUES(ended_at),ended_at), status=VALUES(status),
                  attributes=COALESCE(VALUES(attributes),attributes), mirror_status='pending'""",
                        (span_id, span.get("trace_id"), span.get("task_key"), span.get("correlation_id"),
                         span_type, span.get("parent_span_id"), span["started_at"], span.get("ended_at"),
                         str(span.get("status") or "ok")[:32], encode(attrs) if attrs is not None else None))

    def record_echo_latency(self, message_id, event_id=None, echo_time=None):
        if message_id in (None, ""):
            return False
        with self.conn.cursor() as cur:
            cur.execute("""SELECT request_id,payload,send_confirmed_at,echoed_at
                FROM yunying_sends WHERE message_id=%s LIMIT 1""", (str(message_id),))
            row = cur.fetchone()
        if not row:
            return False
        if row["echoed_at"] is not None:
            return True
        if row["send_confirmed_at"] is None:
            # The QQ echo may race the HTTP response. finish_send() retries
            # this lookup after persisting the API confirmation timestamp.
            return False
        payload = decode(row["payload"]) or {}
        echo_time = echo_time or _now()
        if echo_time < row["send_confirmed_at"]:
            echo_time = row["send_confirmed_at"]
        self.record_latency_span({
            "span_id": f"qq-echo:{row['request_id']}", "trace_id": payload.get("_trace_id"),
            "task_key": payload.get("_task_key"), "correlation_id": row["request_id"],
            "span_type": "qq_echo", "started_at": row["send_confirmed_at"],
            "ended_at": echo_time, "status": "echoed",
            "attributes": {"messageSent": True},
        })
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE yunying_sends SET echoed_at=COALESCE(echoed_at,%s),
                echo_event_id=COALESCE(echo_event_id,%s) WHERE request_id=%s""",
                        (echo_time, event_id, row["request_id"]))
        return True
