-- Durable, privacy-preserving latency outbox.  The DSH JSONL remains the
-- source of truth for the Agent. These rows only contain bounded timings and
-- safe dimensions that can be mirrored to the account site's AI database.
CREATE TABLE IF NOT EXISTS yunying_latency_spans (
  span_id VARCHAR(160) NOT NULL PRIMARY KEY,
  trace_id VARCHAR(64) NULL,
  task_key VARCHAR(191) NULL,
  correlation_id VARCHAR(128) NULL,
  span_type VARCHAR(64) NOT NULL,
  parent_span_id VARCHAR(160) NULL,
  started_at DATETIME(6) NOT NULL,
  ended_at DATETIME(6) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'ok',
  attributes JSON NULL,
  mirror_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  KEY latency_pending (mirror_status, updated_at),
  KEY latency_trace (trace_id, started_at),
  KEY latency_correlation (correlation_id),
  KEY latency_task (task_key, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Link an outbound API request to a later OneBot message_sent echo without
-- putting message content in the latency data.
SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_sends' AND COLUMN_NAME='trace_id') > 0,
  'SELECT 1', 'ALTER TABLE yunying_sends ADD COLUMN trace_id VARCHAR(64) NULL AFTER payload');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_ingress' AND COLUMN_NAME='echo_message_id') > 0,
  'SELECT 1', 'ALTER TABLE yunying_ingress ADD COLUMN echo_message_id VARCHAR(64) NULL AFTER event_id');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_ingress' AND INDEX_NAME='ingress_echo_message') > 0,
  'SELECT 1', 'ALTER TABLE yunying_ingress ADD KEY ingress_echo_message (echo_message_id,id)');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_sends' AND COLUMN_NAME='task_key') > 0,
  'SELECT 1', 'ALTER TABLE yunying_sends ADD COLUMN task_key VARCHAR(191) NULL AFTER trace_id');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_sends' AND INDEX_NAME='sends_message_id') > 0,
  'SELECT 1', 'ALTER TABLE yunying_sends ADD KEY sends_message_id (message_id,updated_at)');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_sends' AND COLUMN_NAME='send_confirmed_at') > 0,
  'SELECT 1', 'ALTER TABLE yunying_sends ADD COLUMN send_confirmed_at DATETIME(6) NULL AFTER task_key');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_sends' AND COLUMN_NAME='echoed_at') > 0,
  'SELECT 1', 'ALTER TABLE yunying_sends ADD COLUMN echoed_at DATETIME(6) NULL AFTER send_confirmed_at');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;

SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_sends' AND COLUMN_NAME='echo_event_id') > 0,
  'SELECT 1', 'ALTER TABLE yunying_sends ADD COLUMN echo_event_id VARCHAR(96) NULL AFTER echoed_at');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
