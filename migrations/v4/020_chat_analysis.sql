-- Bot-owned daily chat analysis. Raw message_logs and the AI ledger stay authoritative, nothing here rewrites them.
CREATE TABLE IF NOT EXISTS chat_analysis_settings (
  platform VARCHAR(32) NOT NULL,
  conversation_type VARCHAR(16) NOT NULL,
  external_id VARCHAR(40) NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
  updated_by VARCHAR(80) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY(platform,conversation_type,external_id),
  KEY chat_analysis_enabled(enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS chat_analysis_reports (
  id CHAR(36) PRIMARY KEY,
  platform VARCHAR(32) NOT NULL,
  conversation_type VARCHAR(16) NOT NULL,
  external_id VARCHAR(40) NOT NULL,
  period_start DATE NOT NULL,
  timezone VARCHAR(64) NOT NULL,
  current_run_id CHAR(36) NULL,
  display_revision BIGINT NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY chat_analysis_period(platform,conversation_type,external_id,period_start,timezone),
  KEY chat_analysis_day(period_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS chat_analysis_runs (
  id CHAR(36) PRIMARY KEY,
  report_id CHAR(36) NOT NULL,
  request_key VARCHAR(128) NOT NULL UNIQUE,
  retry_of CHAR(36) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  analysis VARCHAR(16) NULL,
  actor VARCHAR(80) NOT NULL,
  config JSON NOT NULL,
  period_from DATETIME(3) NOT NULL,
  period_to DATETIME(3) NOT NULL,
  dsh_session_id CHAR(36) NOT NULL UNIQUE,
  lease_token CHAR(36) NULL,
  lease_until DATETIME(6) NULL,
  available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  data_cutoff DATETIME(6) NULL,
  manifest JSON NULL,
  scan_through BIGINT NOT NULL DEFAULT 0,
  phase VARCHAR(32) NOT NULL DEFAULT 'queued',
  statistics JSON NULL,
  draft JSON NULL,
  result JSON NULL,
  markdown MEDIUMTEXT NULL,
  error VARCHAR(255) NULL,
  display_revision BIGINT NOT NULL DEFAULT 0,
  started_at DATETIME(6) NULL,
  ended_at DATETIME(6) NULL,
  accounting_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY chat_analysis_queue(status,available_at,created_at),
  KEY chat_analysis_versions(report_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS chat_analysis_messages (
  run_id CHAR(36) NOT NULL,
  ordinal BIGINT NOT NULL,
  ref VARCHAR(64) NOT NULL,
  occurred_at DATETIME(3) NOT NULL,
  sender_alias VARCHAR(16) NOT NULL,
  payload JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  PRIMARY KEY(run_id,ordinal),
  UNIQUE KEY chat_analysis_message(run_id,ref),
  KEY chat_analysis_sender(run_id,sender_alias,ordinal)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS chat_analysis_receipts (
  run_id CHAR(36) NOT NULL,
  ref VARCHAR(64) NOT NULL,
  PRIMARY KEY(run_id,ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS chat_analysis_calls (
  record_id VARCHAR(128) PRIMARY KEY,
  run_id CHAR(36) NOT NULL,
  phase VARCHAR(32) NOT NULL,
  task_key VARCHAR(191) NOT NULL,
  KEY chat_analysis_call_run(run_id,record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
