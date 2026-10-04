-- V3 is_chat_mode selected unsolicited/direct chat, never reinterpret it as permission.
-- Preserve its values for website compatibility and V3 rollback, V4 uses an explicit pause.
-- Guard the additive ALTER so a crash before checksum registration is safe to retry.
CREATE TABLE IF NOT EXISTS group_configs (
  group_id BIGINT NOT NULL PRIMARY KEY,
  group_name VARCHAR(255) DEFAULT '',
  is_chat_mode TINYINT(1) DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
SET @yunying_group_pause_ddl = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()
    AND TABLE_NAME='group_configs' AND COLUMN_NAME='social_paused') > 0,
  'SELECT 1',
  'ALTER TABLE group_configs ADD COLUMN social_paused TINYINT(1) NOT NULL DEFAULT 0'
);
PREPARE yunying_group_pause_stmt FROM @yunying_group_pause_ddl;
EXECUTE yunying_group_pause_stmt;
DEALLOCATE PREPARE yunying_group_pause_stmt;
