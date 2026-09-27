#!/usr/bin/env python3
"""Rewrite message_logs media paths after the NAS container migration.

Before the migration the bot stored host-absolute paths pointing at the VM's
CIFS mount (/mnt/media/message_logs/...). Inside the NAS container the same
tree is mounted at /data/message_logs, so rows must be re-anchored or every
existing attachment becomes unreadable to both the bot and the IGNG site.

The rewrite is idempotent, runs inside a single transaction, and refuses to
touch rows that do not carry a known legacy prefix.

Usage:
    python rewrite_media_paths.py                # dry run (default)
    python rewrite_media_paths.py --apply        # perform the rewrite
    python rewrite_media_paths.py --revert --apply   # roll back
"""

import argparse
import os
import sys
from pathlib import Path

try:
    import pymysql
except ImportError:
    sys.exit("pymysql is required: pip install pymysql")

NEW_ROOT = os.getenv("MESSAGE_ROOT", "/data/message_logs")
# Includes the pre-NAS local fallback (runtime/attachments) that a handful of
# rows still reference; those files were merged into the NAS tree first.
LEGACY_PREFIXES = [
    p.strip()
    for p in os.getenv(
        "LEGACY_PATH_PREFIXES",
        "/mnt/media/message_logs,"
        "/vol1/1000/IGNGbot/message_logs,"
        "/home/deploy/igngbot-v3/runtime/attachments",
    ).split(",")
    if p.strip()
]
# The revert target is the prefix the old VM deployment used.
REVERT_PREFIX = os.getenv("REVERT_PREFIX", "/mnt/media/message_logs")
# Substring the IGNG site slices on to derive media URLs. A stored value must
# always contain it, which is why the new root is /data/message_logs rather
# than a bare relative path.
PATH_MARKER = os.getenv("PATH_MARKER", "/message_logs/")

# Columns holding an attachment path, in (table, column, is_json) form.
# The JSON columns embed the same host paths inside nested objects, so they
# need the identical re-anchoring; leaving them stale would keep a second,
# silently-wrong copy of every path in the database.
TARGETS = [
    ("message_logs", "file_url", False),
    ("message_logs", "audio_file_path", False),
    ("message_logs", "attachments_json", True),
    ("message_logs", "message_structure", True),
]


def connect():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.getenv("DB_NAME", "igng_bot"),
        charset="utf8mb4",
        autocommit=False,
    )


def count(conn, table, column, predicate, params=None):
    with conn.cursor() as cur:
        # Pass None (not an empty tuple) so the driver skips %-interpolation;
        # these predicates embed LIKE patterns as escaped literals.
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}", params or None)
        return cur.fetchone()[0]


def report(conn):
    print(f"New root:       {NEW_ROOT}")
    print(f"Legacy prefixes: {LEGACY_PREFIXES}")
    print()
    for table, column, is_json in TARGETS:
        total = count(conn, table, column, f"{column} IS NOT NULL AND {column} <> ''")
        print(f"{table}.{column}: {total} non-empty")
        for prefix in LEGACY_PREFIXES:
            n = count(
                conn, table, column, f"{column} LIKE %s", (f"%{prefix}/%",)
            )
            if n:
                print(f"    {prefix}/... -> {n} rows")
        n_new = count(conn, table, column, f"{column} LIKE %s", (f"%{NEW_ROOT}/%",))
        print(f"    already at new root: {n_new} rows")

        # Rows that match no known prefix are reported, never rewritten: they
        # point at features or roots outside the migrated tree and rewriting
        # them would invent a path that never existed. A JSON blob legitimately
        # contains many non-path values, so it is only inspected when it
        # actually carries a media path marker.
        if is_json:
            # Only inspect JSON blobs that actually carry a media path; the
            # marker is a literal here, so escape its percent signs.
            marker = PATH_MARKER.replace("%", "%%")
            scope = f"{column} LIKE '%{marker}%'"
        else:
            scope = f"{column} IS NOT NULL AND {column} <> ''"
        # Prefixes are interpolated as escaped literals because the JSON scope
        # above already consumed the query's parameterization. The leading and
        # trailing % are required: paths are embedded inside JSON objects, not
        # stored at the start of the value.
        def lit(value):
            return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"

        clauses = " AND ".join(
            [f"{column} NOT LIKE {lit('%' + p + '/%')}" for p in LEGACY_PREFIXES]
            + [f"{column} NOT LIKE {lit('%' + NEW_ROOT + '/%')}"]
        )
        orphans = count(conn, table, column, f"{scope} AND {clauses}")
        if orphans:
            print(f"    UNMATCHED (left untouched): {orphans} rows")
            if not is_json:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT SUBSTRING_INDEX({column}, '/', 4) FROM {table} "
                        f"WHERE {scope} AND {clauses} LIMIT 5"
                    )
                    for (prefix,) in cur.fetchall():
                        print(f"        e.g. {prefix}")


def apply(conn, reverse=False):
    """Rewrite embedded path prefixes with REPLACE.

    REPLACE is used rather than a leading SUBSTRING because the JSON columns
    embed the prefix inside nested objects; it is also naturally idempotent,
    since an already-rewritten value no longer contains the old prefix.
    """
    total_changed = 0
    for table, column, _is_json in TARGETS:
        if reverse:
            pairs = [(f"{NEW_ROOT}/", f"{REVERT_PREFIX}/")]
        else:
            pairs = [(f"{p}/", f"{NEW_ROOT}/") for p in LEGACY_PREFIXES]

        for old, new in pairs:
            if old == new:
                continue
            sql = (
                f"UPDATE {table} SET {column} = REPLACE({column}, %s, %s) "
                f"WHERE {column} LIKE %s"
            )
            params = (old, new, f"%{old}%")
            with conn.cursor() as cur:
                cur.execute(sql, params)
                changed = cur.rowcount
            total_changed += changed
            print(f"{table}.{column}: {changed} rows  {old} -> {new}")
    return total_changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the rewrite")
    parser.add_argument("--revert", action="store_true", help="roll back to the legacy prefix")
    args = parser.parse_args()

    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD"):
        if not os.getenv(key):
            sys.exit(f"{key} must be set")

    conn = connect()
    try:
        print("=== before ===")
        report(conn)
        if not args.apply:
            print("\nDry run only. Re-run with --apply to write changes.")
            return
        print("\n=== applying ===")
        changed = apply(conn, reverse=args.revert)
        conn.commit()
        print(f"\nCommitted. {changed} row(s) updated.")
        print("\n=== after ===")
        report(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
