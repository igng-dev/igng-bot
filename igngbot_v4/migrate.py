"""Additive, checksum-checked migrations; no production data is removed."""
from hashlib import sha256
from pathlib import Path
from igngbot_v3.config import Config
import pymysql


def connect(config=None):
    c = config or Config()
    return pymysql.connect(host=c.DB_HOST, port=c.DB_PORT, user=c.DB_USER,
                           password=c.DB_PASSWORD, database=c.DB_NAME, charset="utf8mb4",
                           autocommit=True, cursorclass=pymysql.cursors.DictCursor,
                           connect_timeout=10, read_timeout=10, write_timeout=10,
                           init_command="SET time_zone = '+00:00'")


def migrate(conn, directory=None):
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
