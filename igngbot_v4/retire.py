"""Explicit, reversible operator retirement. Never called by startup or the model.

Use a verified full SQL backup and stop both runtime owners before --apply.
Archives stay in the existing restricted database; no database grants are widened.
"""
import argparse
import hashlib
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .migrate import connect

LEGACY_TABLES = ("group_personality_configs", "personality_profiles", "context_summaries", "system_prompts")
MEDIA_COLUMNS = ("file_url", "file_type", "audio_file_path")
CALL_PROMPTS = {"system_prompt": "system_prompt_hash", "user_prompt": "user_prompt_hash", "thinking_content": "thinking_hash"}
CALL_INDEX = ("id", "group_id", "sender_id", "task_id", "sender_name", "message_text", "call_type", "model", "duration_ms", "success", "error_message", "created_at")


def serial(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=serial)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def identifier(name):
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("invalid SQL identifier")
    return "`" + name + "`"


def exists(conn, table):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) n FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        return bool(cur.fetchone()["n"])


def canonical_schema(ddl):
    """Ignore only a redundant charset already determined by COLLATE.

    SHOW CREATE after a MySQL dump/restore can add CHARACTER SET. Quoted
    defaults/comments/identifiers are preserved byte for byte. Column metadata
    is compared independently; no type, index, collation or default is ignored.
    """
    pattern = r"'(?:\\.|''|[^'])*'|`(?:``|[^`])*`|\bCHARACTER SET ([A-Za-z0-9_]+) COLLATE ([A-Za-z0-9_]+)"
    def replace(match):
        charset, collation = match.group(1), match.group(2)
        if charset and collation.startswith(charset + "_"):
            return "COLLATE " + collation
        return match.group(0)
    return re.sub(pattern, replace, ddl)


def table_snapshot(conn, table):
    """Portable digest of actual rows, including NULL and JSON strings, by PK."""
    with conn.cursor() as cur:
        cur.execute("SHOW CREATE TABLE " + identifier(table))
        ddl = cur.fetchone()["Create Table"]
        cur.execute("SELECT COLUMN_NAME,ORDINAL_POSITION,COLUMN_TYPE,COLUMN_DEFAULT,IS_NULLABLE,CHARACTER_SET_NAME,COLLATION_NAME,EXTRA,COLUMN_COMMENT,GENERATION_EXPRESSION FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (table,))
        column_hash = digest(cur.fetchall())
        cur.execute("SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX", (table,))
        keys = [r["COLUMN_NAME"] for r in cur.fetchall()]
        if not keys:
            raise ValueError("snapshot requires a primary key")
        cur.execute("SELECT * FROM " + identifier(table) + " ORDER BY " + ",".join(map(identifier, keys)))
        rows = list(cur.fetchall())
    return {"table": table, "create_sql": ddl, "rows": rows, "row_count": len(rows), "content_sha256": digest(rows), "schema_sha256": hashlib.sha256(canonical_schema(ddl).encode()).hexdigest(), "columns_sha256": column_hash}


def server_identity(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW VARIABLES LIKE 'server_uuid'")
        row = cur.fetchone()
        if row:
            return row["Value"]
        # Development MariaDB has no server_uuid. Production proofs use MySQL's UUID.
        cur.execute("SELECT @@hostname hostname,@@port port,@@server_id server_id")
        return digest(cur.fetchone())


def prove_restore(source, restored, backup, output):
    """Read-only comparison, including every table/row and columns/index definitions.

    The restored connection must point at an isolated disposable service, never a
    second schema on the production service. Connection creation belongs to ops.
    """
    with source.cursor() as cur:
        cur.execute("SELECT DATABASE() db")
        origin = cur.fetchone()
    origin["uuid"] = server_identity(source)
    if server_identity(restored) == origin["uuid"]:
        raise ValueError("restore proof must use a different MySQL server")
    with source.cursor() as cur:
        cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME")
        names = [r["TABLE_NAME"] for r in cur.fetchall()]
    tables = {}
    for name in names:
        a, b = table_snapshot(source, name), table_snapshot(restored, name)
        # SHOW CREATE includes restored auto_increment, charset, collation and indexes.
        if a["content_sha256"] != b["content_sha256"] or a["schema_sha256"] != b["schema_sha256"] or a["columns_sha256"] != b["columns_sha256"]:
            raise ValueError("restored table differs: " + name)
        tables[name] = {k: a[k] for k in ("row_count", "content_sha256", "schema_sha256", "columns_sha256")}
    backup = Path(backup)
    proof = {"format": 2, "restore_verified": True, "database": origin["db"],
             "server_uuid": origin["uuid"], "backup_file": backup.name,
             "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "tables": tables,
             "verified_at": datetime.now().isoformat()}
    Path(output).write_text(encoded(proof) + "\n")
    Path(output).chmod(0o600)
    return {"restored_tables": len(tables), "rows": sum(t["row_count"] for t in tables.values())}


class Retirement:
    def __init__(self, conn, proof=None):
        self.conn = conn
        self.proof = proof
        self.batch = str(uuid.uuid4())

    def quiet(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT IS_USED_LOCK(CONCAT('yunying-onebot:',DATABASE())) bot,IS_USED_LOCK(CONCAT('yunying-dsh:',DATABASE())) dsh")
            if any(cur.fetchone().values()):
                raise RuntimeError("stop bot and YunYing before applying retirement or restore")
            cur.execute("SELECT COUNT(*) n FROM yunying_ai_records WHERE mirror_status<>'mirrored'")
            if cur.fetchone()["n"]:
                raise RuntimeError("drain AI accounting outbox first")
            cur.execute("SELECT COUNT(*) n FROM yunying_sends WHERE status IN ('pending','sending','unknown')")
            if cur.fetchone()["n"]:
                raise RuntimeError("resolve pending/unknown sends before retirement")

    def verify_backup(self):
        if not self.proof:
            raise ValueError("--apply requires --proof from an isolated restore")
        path = Path(self.proof)
        self.evidence = json.loads(path.read_text())
        p = self.evidence
        if p.get("format") != 2 or p.get("restore_verified") is not True:
            raise ValueError("invalid restore proof")
        archive = path.parent / p["backup_file"]
        if archive.parent.resolve() != path.parent.resolve() or hashlib.sha256(archive.read_bytes()).hexdigest() != p["backup_sha256"]:
            raise ValueError("SQL backup checksum mismatch")
        with self.conn.cursor() as cur:
            cur.execute("SELECT DATABASE() db")
            row = cur.fetchone()
        row["uuid"] = server_identity(self.conn)
        if p["database"] != row["db"] or p["server_uuid"] != row["uuid"]:
            raise ValueError("proof is for a different database/server")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def verified_snapshot(self, table):
        snap = table_snapshot(self.conn, table)
        recorded = self.evidence["tables"].get(table)
        if recorded != {k: snap[k] for k in ("row_count", "content_sha256", "schema_sha256", "columns_sha256")}:
            raise ValueError("backup is stale for " + table)
        return snap

    def start(self, stage):
        self.batch = str(uuid.uuid4())
        self.quiet()
        proof_hash = self.verify_backup()
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO yunying_retirement_batches (batch_id,stage,proof_sha256,detail) VALUES (%s,%s,%s,'{}')", (self.batch, stage, proof_hash))

    def finish(self, result):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE yunying_retirement_batches SET status='complete',detail=%s,completed_at=UTC_TIMESTAMP(6) WHERE batch_id=%s", (encoded(result), self.batch))
        return result

    def archive_table(self, snap, rows=True):
        with self.conn.cursor() as cur:
            cur.execute("SELECT content_sha256 FROM yunying_legacy_tables WHERE table_name=%s", (snap["table"],))
            prior = cur.fetchone()
            if prior:
                if prior["content_sha256"] != snap["content_sha256"]:
                    raise ValueError("existing archive differs")
                return

            cur.execute("INSERT INTO yunying_legacy_tables (table_name,create_sql,rows_json,row_count,content_sha256,batch_id) VALUES (%s,%s,%s,%s,%s,%s)",
                        (snap["table"], snap["create_sql"], encoded(snap["rows"]) if rows else None, snap["row_count"], snap["content_sha256"], self.batch))

    def legacy_tables(self, apply=False):
        names = [n for n in LEGACY_TABLES if exists(self.conn, n)]
        if not apply:
            return {"stage": "legacy-tables", "tables": {n: table_snapshot(self.conn, n)["row_count"] for n in names}}
        self.start("legacy-tables")
        # Validate the entire stage before its first DDL. Per-table archives make DDL retries reversible.
        snapshots = [self.verified_snapshot(n) for n in names]
        for snap in snapshots:
            self.archive_table(snap)
            with self.conn.cursor() as cur:
                cur.execute("SELECT rows_json,content_sha256 FROM yunying_legacy_tables WHERE table_name=%s", (snap["table"],))
                archived = cur.fetchone()
                if digest(json.loads(archived["rows_json"])) != snap["content_sha256"]:
                    raise ValueError("legacy archive verification failed")
                cur.execute("DROP TABLE " + identifier(snap["table"]))
        return self.finish({"retired_tables": names, "archived_rows": sum(s["row_count"] for s in snapshots)})

    def hydrate_calls(self, rows):
        with self.conn.cursor() as cur:
            cur.execute("SELECT content_sha256,content FROM yunying_prompt_blobs")
            blobs = {r["content_sha256"]: r["content"] for r in cur.fetchall()}
        # Read and verify each distinct body once, not three roundtrips per old call.
        for h, body in blobs.items():
            if hashlib.sha256(body.encode()).hexdigest() != h:
                raise ValueError("corrupt prompt blob")
        restored = []
        for row in rows:
            metadata = json.loads(row["metadata"])
            for name, link in CALL_PROMPTS.items():
                h = row[link]
                if h is not None and h not in blobs:
                    raise ValueError("missing prompt blob")
                metadata[name] = blobs[h] if h is not None else None
            restored.append(metadata)
        return restored

    def call_history(self, apply=False):
        if not exists(self.conn, "call_logs"):
            return {"stage": "call-history", "already_retired": True}
        if not apply:
            return {"stage": "call-history", "calls": table_snapshot(self.conn, "call_logs")["row_count"]}
        self.start("call-history")
        snap = self.verified_snapshot("call_logs")
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM yunying_call_history")
            prior_count = cur.fetchone()["n"]
        self.conn.begin()
        try:
            with self.conn.cursor() as cur:
                blobs, calls = {}, []
                for row in ([] if prior_count else snap["rows"]):
                    meta = dict(row)
                    hashes = []
                    for name in CALL_PROMPTS:
                        value = meta.pop(name, None)
                        h = hashlib.sha256(value.encode()).hexdigest() if value is not None else None
                        hashes.append(h)
                        if h:
                            blobs[h] = (h, value, len(value.encode()))
                    calls.append(tuple(row.get(k) for k in CALL_INDEX) + (encoded(meta), *hashes, self.batch))
                cur.executemany("INSERT IGNORE INTO yunying_prompt_blobs (content_sha256,content,byte_length) VALUES (%s,%s,%s)", list(blobs.values()))
                columns = CALL_INDEX + ("metadata", "system_prompt_hash", "user_prompt_hash", "thinking_hash", "batch_id")
                cur.executemany("INSERT INTO yunying_call_history (" + ",".join(columns) + ") VALUES (" + ",".join(["%s"] * len(columns)) + ")", calls)
                self.archive_table(snap, rows=False)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM yunying_call_history ORDER BY id")
            restored = self.hydrate_calls(cur.fetchall())
        if digest(restored) != snap["content_sha256"]:
            raise ValueError("call history content differs; original table retained")
        with self.conn.cursor() as cur:
            cur.execute("DROP TABLE call_logs")
            cur.execute("SELECT COUNT(*) n,SUM(byte_length) bytes FROM yunying_prompt_blobs")
            blobs = cur.fetchone()
        return self.finish({"archived_calls": snap["row_count"], "prompt_blobs": blobs})

    def media_columns(self, apply=False):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,COLUMN_COMMENT,COLLATION_NAME,ORDINAL_POSITION FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND COLUMN_NAME IN ('file_url','file_type','audio_file_path') ORDER BY ORDINAL_POSITION")
            columns = cur.fetchall()
        if not columns:
            return {"stage": "media-columns", "already_retired": True}
        if len(columns) != 3:
            raise ValueError("unexpected partial media schema")
        with self.conn.cursor() as cur:
            cur.execute("SELECT COLUMN_NAME,ORDINAL_POSITION FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' ORDER BY ORDINAL_POSITION")
            order = {r["ORDINAL_POSITION"]: r["COLUMN_NAME"] for r in cur.fetchall()}
            for col in columns:
                col["after"] = order.get(col["ORDINAL_POSITION"] - 1)
            cur.execute("SELECT id,file_url,file_type,audio_file_path,attachments_json FROM message_logs ORDER BY id")
            rows = list(cur.fetchall())
        for row in rows:
            media = json.loads(row["attachments_json"] or "[]")
            paths = {a.get("stored_path") for a in media if isinstance(a, dict)}
            for col in ("file_url", "audio_file_path"):
                if row[col] and row[col] not in paths:
                    raise ValueError("uncovered legacy attachment path; keep original columns")
            if row["file_type"] and row["file_url"] and not any(a.get("stored_path") == row["file_url"] and a.get("type") == row["file_type"] for a in media):
                raise ValueError("attachment type differs; keep original columns")
        if not apply:
            return {"stage": "media-columns", "covered_rows": len(rows), "columns": list(MEDIA_COLUMNS)}
        self.start("media-columns")
        self.verified_snapshot("message_logs")
        archive = [{"message_log_id": r["id"], **{c: r[c] for c in MEDIA_COLUMNS}} for r in rows if any(r[c] is not None for c in MEDIA_COLUMNS)]
        with self.conn.cursor() as cur:
            cur.execute("SELECT content_sha256 FROM yunying_legacy_tables WHERE table_name='message_logs.media'")
            prior = cur.fetchone()
            if prior and prior["content_sha256"] != digest(archive):
                raise ValueError("existing media archive differs")
        self.conn.begin()
        try:
            with self.conn.cursor() as cur:
                cur.executemany("INSERT IGNORE INTO yunying_media_legacy (message_log_id,file_url,file_type,audio_file_path,batch_id) VALUES (%s,%s,%s,%s,%s)", [(*r.values(), self.batch) for r in archive])
                cur.execute("SELECT message_log_id,file_url,file_type,audio_file_path FROM yunying_media_legacy ORDER BY message_log_id")
                if digest(cur.fetchall()) != digest(archive):
                    raise ValueError("media archive mismatch")
                cur.execute("INSERT IGNORE INTO yunying_legacy_tables (table_name,create_sql,rows_json,row_count,content_sha256,batch_id) VALUES ('message_logs.media',%s,NULL,%s,%s,%s)", (encoded(columns), len(archive), digest(archive), self.batch))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        # One atomic ALTER removes all three columns; original values are retained above.
        with self.conn.cursor() as cur:
            cur.execute("ALTER TABLE message_logs DROP COLUMN file_url,DROP COLUMN file_type,DROP COLUMN audio_file_path")
        return self.finish({"retired_columns": list(MEDIA_COLUMNS), "archived_media_rows": len(archive), "audio_transcript": "dropped"})

    def ingress(self, apply=False, days=30):
        if days < 30:
            raise ValueError("minimum ingress retention is 30 days")
        with self.conn.cursor() as cur:
            cur.execute("SELECT event_id,raw_event,prepared_event FROM yunying_ingress WHERE recording_status='recorded' AND delivery_status='delivered' AND payload_archived_at IS NULL AND delivered_at < DATE_SUB(UTC_TIMESTAMP(6),INTERVAL %s DAY) ORDER BY id", (days,))
            rows = list(cur.fetchall())
        if not apply:
            return {"stage": "ingress", "eligible": len(rows), "days": days}
        self.start("ingress")
        self.verified_snapshot("yunying_ingress")
        # Session mappings/state, event source payloads, sends and AI idempotency links remain live.
        with self.conn.cursor() as cur:
            cur.execute("SELECT conversation_key,dsh_session_id,next_seq,social_state FROM yunying_sessions ORDER BY conversation_key")
            checkpoint = digest(cur.fetchall())
        self.conn.begin()
        try:
            with self.conn.cursor() as cur:
                for row in rows:
                    body = {k: json.loads(row[k]) if row[k] else None for k in ("raw_event", "prepared_event")}
                    h = digest(body)
                    cur.execute("INSERT INTO yunying_ingress_archive (event_id,raw_event,prepared_event,payload_sha256,batch_id) VALUES (%s,%s,%s,%s,%s)", (row["event_id"], row["raw_event"], row["prepared_event"], h, self.batch))
                    cur.execute("SELECT raw_event,prepared_event FROM yunying_ingress_archive WHERE event_id=%s", (row["event_id"],))
                    saved = cur.fetchone()
                    if digest({k: json.loads(saved[k]) if saved[k] else None for k in body}) != h:
                        raise ValueError("ingress archive mismatch")
                    cur.execute("UPDATE yunying_ingress SET raw_event=JSON_OBJECT('archive',%s,'sha256',%s),prepared_event=NULL,payload_archived_at=UTC_TIMESTAMP(6) WHERE event_id=%s AND delivery_status='delivered' AND recording_status='recorded'", (self.batch, h, row["event_id"]))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.finish({"archived_ingress": len(rows), "days": days, "session_checkpoint_sha256": checkpoint, "events_sends_ai_memory": "unchanged"})

    def restore(self, apply=False):
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM yunying_legacy_tables ORDER BY table_name")
            tables = list(cur.fetchall())
        if not apply:
            return {"stage": "restore", "tables": [r["table_name"] for r in tables]}
        self.quiet()
        for table in tables:
            name = table["table_name"]
            if name == "message_logs.media":
                with self.conn.cursor() as cur:
                    cur.execute("SELECT message_log_id,file_url,file_type,audio_file_path FROM yunying_media_legacy ORDER BY message_log_id")
                    rows = list(cur.fetchall())
                    if digest(rows) != table["content_sha256"]:
                        raise ValueError("media archive corrupt")
                    cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND COLUMN_NAME IN ('file_url','file_type','audio_file_path')")
                    present = {r["COLUMN_NAME"] for r in cur.fetchall()}
                    cur.execute("SELECT batch_id,status FROM yunying_retirement_batches WHERE stage='restore-media' ORDER BY created_at DESC LIMIT 1")
                    progress = cur.fetchone()
                    if progress and progress["status"] == 'complete' and len(present) == 3:
                        # V3/operator writes after a completed restore must never be overwritten.
                        continue
                    if not progress or progress["status"] == 'complete':
                        progress = {"batch_id": str(uuid.uuid4()), "status": "prepared"}
                        cur.execute("INSERT INTO yunying_retirement_batches (batch_id,stage,proof_sha256,detail) VALUES (%s,'restore-media',%s,'{}')", (progress["batch_id"], table["content_sha256"]))
                    if present:
                        cur.execute("SELECT id," + ",".join(map(identifier, sorted(present))) + " FROM message_logs WHERE id IN (SELECT message_log_id FROM yunying_media_legacy)")
                        existing = {r["id"]: r for r in cur.fetchall()}
                        for row in rows:
                            for col in present:
                                value = existing.get(row["message_log_id"], {}).get(col)
                                if value is not None and value != row[col]:
                                    raise ValueError("existing media differs; preserve operator/V3 writes")
                    for col in json.loads(table["create_sql"]):
                        cur.execute("SELECT COUNT(*) n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND COLUMN_NAME=%s", (col["COLUMN_NAME"],))
                        if not cur.fetchone()["n"]:
                            # Original retired columns are nullable with a NULL default.
                            if col["IS_NULLABLE"] != 'YES' or col["COLUMN_DEFAULT"] not in (None, 'NULL'):
                                raise ValueError("unsupported legacy column definition")
                            definition = col["COLUMN_TYPE"] + (" COLLATE " + identifier(col["COLLATION_NAME"]) if col["COLLATION_NAME"] else "")
                            position = " AFTER " + identifier(col["after"]) if col["after"] else " FIRST"
                            cur.execute("ALTER TABLE message_logs ADD COLUMN " + identifier(col["COLUMN_NAME"]) + " " + definition + " NULL COMMENT %s" + position, (col["COLUMN_COMMENT"],))
                    self.conn.begin()
                    try:
                        cur.executemany("UPDATE message_logs SET file_url=%s,file_type=%s,audio_file_path=%s WHERE id=%s", [(r["file_url"], r["file_type"], r["audio_file_path"], r["message_log_id"]) for r in rows])
                        cur.execute("UPDATE yunying_retirement_batches SET status='complete',completed_at=UTC_TIMESTAMP(6),detail=%s WHERE batch_id=%s", (encoded({"restored_media_rows": len(rows)}), progress["batch_id"]))
                        self.conn.commit()
                    except Exception:
                        self.conn.rollback()
                        raise
                continue
            if exists(self.conn, name):
                prior = table_snapshot(self.conn, name)
                if prior["content_sha256"] == table["content_sha256"]:
                    continue
                if prior["row_count"]:
                    raise ValueError("existing table differs; preserve operator/V3 writes: " + name)
                # A prior CREATE may have committed before a failed INSERT transaction.

            if name == "call_logs":
                with self.conn.cursor() as cur:
                    cur.execute("SELECT * FROM yunying_call_history ORDER BY id")
                    rows = self.hydrate_calls(cur.fetchall())
            else:
                rows = json.loads(table["rows_json"])
            if digest(rows) != table["content_sha256"]:
                raise ValueError("archive corrupt: " + name)
            with self.conn.cursor() as cur:
                if not exists(self.conn, name):
                    cur.execute(table["create_sql"])
            self.conn.begin()
            try:
                with self.conn.cursor() as cur:
                    for row in rows:
                        cols = list(row)
                        cur.execute("INSERT INTO " + identifier(name) + " (" + ",".join(map(identifier, cols)) + ") VALUES (" + ",".join(["%s"] * len(cols)) + ")", tuple(row.values()))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM yunying_ingress_archive ORDER BY event_id")
            rows = list(cur.fetchall())
            for row in rows:
                body = {k: json.loads(row[k]) if row[k] else None for k in ("raw_event", "prepared_event")}
                if digest(body) != row["payload_sha256"]:
                    raise ValueError("ingress archive corrupt")
                cur.execute("UPDATE yunying_ingress SET raw_event=%s,prepared_event=%s,payload_archived_at=NULL WHERE event_id=%s AND payload_archived_at IS NOT NULL", (row["raw_event"], row["prepared_event"], row["event_id"]))
        return {"restored_tables": [r["table_name"] for r in tables], "restored_ingress": len(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("legacy-tables", "call-history", "media-columns", "ingress", "restore"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--proof", type=Path)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(CONCAT('yunying-retirement:',DATABASE()),30) owned")
            if cur.fetchone()["owned"] != 1:
                raise RuntimeError("another retirement operator owns the database")
        try:
            retire = Retirement(conn, args.proof)
            method = getattr(retire, args.stage.replace("-", "_"))
            result = method(apply=args.apply, **({"days": args.days} if args.stage == "ingress" else {}))
            print(encoded(result))
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(CONCAT('yunying-retirement:',DATABASE()))")


if __name__ == "__main__":
    main()
