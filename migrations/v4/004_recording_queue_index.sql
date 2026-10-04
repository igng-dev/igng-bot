-- The independent recording worker needs its own pending-work index.
SET @yunying_recording_index = IF((SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()
    AND TABLE_NAME='yunying_ingress' AND INDEX_NAME='ingress_recording') > 0,
    'SELECT 1', 'ALTER TABLE yunying_ingress ADD KEY ingress_recording (recording_status,recording_available_at,id)');
PREPARE yunying_recording_stmt FROM @yunying_recording_index;
EXECUTE yunying_recording_stmt;
DEALLOCATE PREPARE yunying_recording_stmt;
