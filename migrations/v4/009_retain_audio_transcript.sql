-- Additive compatibility migration. audio_transcript remains shared ASR output.
-- If an older schema is missing the column, restore the schema contract before
-- any retirement stage can complete. This migration never deletes transcript data.
SET @yunying_add_transcript = IF((SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs') = 1 AND (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND COLUMN_NAME='audio_transcript') = 0, 'ALTER TABLE message_logs ADD COLUMN audio_transcript TEXT COMMENT ''语音转写文本''', 'SELECT 1');
PREPARE yunying_stmt FROM @yunying_add_transcript;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
