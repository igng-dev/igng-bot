-- Additive support only. Retirement is an explicit operator command, never startup.
CREATE TABLE IF NOT EXISTS yunying_retirement_batches (
  batch_id CHAR(36) PRIMARY KEY,
  stage VARCHAR(32) NOT NULL,
  proof_sha256 CHAR(64) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'prepared',
  detail JSON NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at DATETIME(6) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_legacy_tables (
  table_name VARCHAR(120) PRIMARY KEY,
  create_sql LONGTEXT NOT NULL,
  rows_json LONGTEXT NULL,
  row_count BIGINT UNSIGNED NOT NULL,
  content_sha256 CHAR(64) NOT NULL,
  batch_id CHAR(36) NOT NULL,
  archived_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_prompt_blobs (
  content_sha256 CHAR(64) PRIMARY KEY,
  content LONGTEXT NOT NULL,
  byte_length BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_call_history (
  id BIGINT PRIMARY KEY,
  group_id VARCHAR(50) NOT NULL DEFAULT '',
  sender_id VARCHAR(50) NOT NULL DEFAULT '',
  task_id BIGINT NULL,
  sender_name VARCHAR(100) NULL,
  message_text TEXT NULL,
  call_type VARCHAR(32) NOT NULL,
  model VARCHAR(100) NULL,
  duration_ms INT NULL,
  success TINYINT NULL,
  error_message TEXT NULL,
  created_at DATETIME NULL,
  metadata JSON NOT NULL,
  system_prompt_hash CHAR(64) NULL,
  user_prompt_hash CHAR(64) NULL,
  thinking_hash CHAR(64) NULL,
  batch_id CHAR(36) NOT NULL,
  KEY call_history_time (created_at, id),
  KEY call_history_group (group_id, created_at),
  KEY call_history_model (model),
  KEY call_history_type (call_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_media_legacy (
  message_log_id BIGINT PRIMARY KEY,
  file_url VARCHAR(500) NULL,
  file_type VARCHAR(50) NULL,
  audio_file_path VARCHAR(500) NULL,
  batch_id CHAR(36) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_ingress_archive (
  event_id VARCHAR(96) PRIMARY KEY,
  raw_event JSON NOT NULL,
  prepared_event JSON NULL,
  payload_sha256 CHAR(64) NOT NULL,
  batch_id CHAR(36) NOT NULL,
  archived_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
SET @yunying_ddl = IF((SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='yunying_ingress' AND COLUMN_NAME='payload_archived_at') > 0, 'SELECT 1', 'ALTER TABLE yunying_ingress ADD COLUMN payload_archived_at DATETIME(6) NULL');
PREPARE yunying_stmt FROM @yunying_ddl;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
