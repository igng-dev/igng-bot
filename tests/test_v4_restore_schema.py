from pathlib import Path

from igngbot_v4.retire import canonical_schema

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "v4"
_STATEMENT_HEADS = {"CREATE", "ALTER", "SET", "PREPARE", "EXECUTE", "DEALLOCATE", "UPDATE", "SELECT", "INSERT", "DROP", "DELETE"}


def test_migrations_are_safe_for_the_naive_semicolon_splitter():
    """migrate.py splits each file on ';' and executes every fragment in turn.

    A semicolon inside a SQL comment corrupts the following fragment (it starts
    mid-comment). Guard the whole directory so that can never break startup.
    """
    for source in sorted(_MIGRATIONS.glob("*.sql")):
        for index, fragment in enumerate(source.read_text(encoding="utf-8").split(";")):
            lines = [line.strip() for line in fragment.splitlines()
                     if line.strip() and not line.strip().startswith("--")]
            if not lines:
                continue
            head = lines[0].split()[0].upper()
            assert head in _STATEMENT_HEADS, \
                f"{source.name} fragment {index} starts with {head!r}: {lines[0][:80]}"


def test_mysql_restore_redundant_charset_is_equivalent_but_literals_remain_exact():
    original = "CREATE TABLE `fixture` (\n  `body` text COLLATE utf8mb4_general_ci COMMENT 'CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci'\n) ENGINE=InnoDB"
    restored = original.replace('`body` text COLLATE', '`body` text CHARACTER SET utf8mb4 COLLATE')
    assert canonical_schema(original) == canonical_schema(restored)
    for before, after in [('text','varchar(255)'), ('utf8mb4_general_ci','utf8mb4_bin'), ('InnoDB','MyISAM'), ("COMMENT '", "COMMENT 'changed ")]:
        assert canonical_schema(original) != canonical_schema(original.replace(before, after))
    literal = "`body` text DEFAULT 'CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci'"
    assert canonical_schema(literal) == literal
    escaped = "`body` text COMMENT 'an escaped \\' CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci'"
    assert canonical_schema(escaped) == escaped
