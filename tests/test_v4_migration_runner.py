"""Unit coverage for the safe/destructive V4 migration boundary."""
from hashlib import sha256

import pytest

from igngbot_v4.migrate import migrate


class FakeCursor:
    def __init__(self, recorded=None):
        self.recorded = recorded or {}
        self.calls = []
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if sql.startswith("SELECT GET_LOCK"):
            self._result = {"acquired": 1}
        elif sql.startswith("SELECT RELEASE_LOCK"):
            self._result = None
        elif sql.startswith("SELECT checksum"):
            self._result = self.recorded.get(args[0])
        else:
            self._result = None

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, recorded=None):
        self.cursor_obj = FakeCursor(recorded)

    def cursor(self):
        return self.cursor_obj


def _write_migration(directory, name, sql):
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def _executed_sql(cursor):
    return [sql for sql, _ in cursor.calls]


def test_safe_startup_skips_007_without_recording_it_but_applies_009(tmp_path):
    _write_migration(tmp_path, "007_drop_audio_transcript.sql", "ALTER TABLE message_logs DROP COLUMN audio_transcript;")
    _write_migration(tmp_path, "009_retain_audio_transcript.sql", "ALTER TABLE message_logs ADD COLUMN audio_transcript TEXT;")

    conn = FakeConnection()
    migrate(conn, directory=tmp_path)

    sql = _executed_sql(conn.cursor_obj)
    assert "ALTER TABLE message_logs DROP COLUMN audio_transcript" not in " ".join(sql)
    assert "ALTER TABLE message_logs ADD COLUMN audio_transcript TEXT" in " ".join(sql)
    inserted_versions = [args[0] for statement, args in conn.cursor_obj.calls
                         if statement.startswith("INSERT INTO yunying_schema_migrations")]
    assert inserted_versions == ["009_retain_audio_transcript.sql"]


def test_safe_startup_still_validates_checksum_for_previously_recorded_007(tmp_path):
    source = _write_migration(tmp_path, "007_drop_audio_transcript.sql", "ALTER TABLE message_logs DROP COLUMN audio_transcript;")
    digest = sha256(source.read_bytes()).hexdigest()
    conn = FakeConnection({"007_drop_audio_transcript.sql": {"checksum": digest}})

    migrate(conn, directory=tmp_path)

    sql = _executed_sql(conn.cursor_obj)
    assert "ALTER TABLE message_logs DROP COLUMN audio_transcript" not in " ".join(sql)


def test_safe_startup_rejects_checksum_drift_for_previously_recorded_007(tmp_path):
    source = _write_migration(tmp_path, "007_drop_audio_transcript.sql", "ALTER TABLE message_logs DROP COLUMN audio_transcript;")
    conn = FakeConnection({"007_drop_audio_transcript.sql": {"checksum": "0" * 64}})

    with pytest.raises(RuntimeError, match="checksum mismatch: 007_drop_audio_transcript.sql"):
        migrate(conn, directory=tmp_path)


def test_operator_mode_executes_destructive_007_and_records_009(tmp_path):
    _write_migration(tmp_path, "007_drop_audio_transcript.sql", "ALTER TABLE message_logs DROP COLUMN audio_transcript;")
    _write_migration(tmp_path, "009_retain_audio_transcript.sql", "ALTER TABLE message_logs ADD COLUMN audio_transcript TEXT;")

    conn = FakeConnection()
    migrate(conn, directory=tmp_path, include_destructive=True)

    sql = _executed_sql(conn.cursor_obj)
    joined = " ".join(sql)
    assert "ALTER TABLE message_logs DROP COLUMN audio_transcript" in joined
    assert "ALTER TABLE message_logs ADD COLUMN audio_transcript TEXT" in joined
    inserted_versions = [args[0] for statement, args in conn.cursor_obj.calls
                         if statement.startswith("INSERT INTO yunying_schema_migrations")]
    assert inserted_versions == ["007_drop_audio_transcript.sql", "009_retain_audio_transcript.sql"]
