-- Additive: display-name snapshot at ingest and a per-group day-range index for chat analysis.
-- Old rows keep NULL, nothing is backfilled or rewritten. Both steps are skipped when already present.
SET @yunying_add_sender_name = IF((SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs') = 1 AND (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND COLUMN_NAME='sender_name') = 0, 'ALTER TABLE message_logs ADD COLUMN sender_name VARCHAR(120) NULL COMMENT ''发送时的群名片/昵称快照''', 'SELECT 1');
PREPARE yunying_stmt FROM @yunying_add_sender_name;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
SET @yunying_add_group_created = IF((SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs') = 1 AND (SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND INDEX_NAME='idx_group_created') = 0, 'ALTER TABLE message_logs ADD INDEX idx_group_created (group_id, created_at)', 'SELECT 1');
PREPARE yunying_stmt FROM @yunying_add_group_created;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
