from igngbot_v4.retire import canonical_schema


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
