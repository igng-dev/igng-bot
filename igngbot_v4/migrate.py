"""Checksum-checked startup migrations; destructive retirement is operator-only."""
from hashlib import sha256
from pathlib import Path
from igngbot_shared.config import Config
import pymysql


# Keep the historical migration identity for checksum compatibility, but never
# execute it during ordinary V4 startup. Media retirement is separately gated
# and archived by igngbot_v4.retire.
DESTRUCTIVE_MIGRATIONS = frozenset({"007_drop_audio_transcript.sql"})


def connect(config=None):
    c = config or Config()
    return pymysql.connect(host=c.DB_HOST, port=c.DB_PORT, user=c.DB_USER,
                           password=c.DB_PASSWORD, database=c.DB_NAME, charset="utf8mb4",
                           autocommit=True, cursorclass=pymysql.cursors.DictCursor,
                           ssl=Config.db_ssl_context(),
                           connect_timeout=10, read_timeout=10, write_timeout=10,
                           init_command="SET time_zone = '+00:00'")


def migrate(conn, directory=None, include_destructive=False):
    directory = Path(directory or Path(__file__).resolve().parents[1] / "migrations/v4")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS yunying_schema_migrations (version VARCHAR(120) PRIMARY KEY, checksum CHAR(64) NOT NULL, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB")
        cur.execute("SELECT GET_LOCK('igngbot-v4-migrations', 30) AS acquired")
        if cur.fetchone()["acquired"] != 1:
            raise RuntimeError("another migration holds the lock")
        try:
            for source in sorted(directory.glob("*.sql")):
                data = source.read_text(encoding="utf-8")
                digest = sha256(data.encode()).hexdigest()
                cur.execute("SELECT checksum FROM yunying_schema_migrations WHERE version=%s", (source.name,))
                old = cur.fetchone()
                if old:
                    if old["checksum"] != digest:
                        raise RuntimeError(f"migration checksum mismatch: {source.name}")
                    continue
                if source.name in DESTRUCTIVE_MIGRATIONS and not include_destructive:
                    continue
                # Migration vocabulary is plain SQL, with no stored procedures/string semicolons.
                for statement in data.split(";"):
                    if statement.strip():
                        cur.execute(statement)
                cur.execute("INSERT INTO yunying_schema_migrations (version, checksum) VALUES (%s,%s)", (source.name, digest))
        finally:
            cur.execute("SELECT RELEASE_LOCK('igngbot-v4-migrations')")


if __name__ == "__main__":
    with connect() as conn:
        migrate(conn)
    print("V4 additive migrations applied")
