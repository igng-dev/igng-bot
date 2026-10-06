-- Additive only. Revision sources move next to the version they belong to;
-- identity/binding/audit retirement is a separate operator stage, never startup.
SET @yunying_memory_sources = IF((SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='memory_versions' AND COLUMN_NAME='sources') > 0, 'SELECT 1', 'ALTER TABLE memory_versions ADD COLUMN sources JSON NULL');
PREPARE yunying_stmt FROM @yunying_memory_sources;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
