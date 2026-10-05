-- OCR/ASR removal. Guarded single drop of the retired transcript column.
-- Destructive: take a full SQL backup before deploying. The values are
-- historical derived text and are no longer written or read.
SET @yunying_drop_transcript = IF((SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='message_logs' AND COLUMN_NAME='audio_transcript') > 0, 'ALTER TABLE message_logs DROP COLUMN audio_transcript', 'SELECT 1');
PREPARE yunying_stmt FROM @yunying_drop_transcript;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
