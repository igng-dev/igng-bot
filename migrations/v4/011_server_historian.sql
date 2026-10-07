-- Bot-owned report storage. MC facts and the existing AI ledger stay authoritative.
CREATE TABLE IF NOT EXISTS historian_reports (
  id CHAR(36) PRIMARY KEY,
  server_id INT NOT NULL,
  kind VARCHAR(8) NOT NULL,
  period_start DATE NOT NULL,
  timezone VARCHAR(64) NOT NULL,
  current_run_id CHAR(36) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY historian_period(server_id,kind,period_start,timezone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS historian_runs (
  id CHAR(36) PRIMARY KEY,
  report_id CHAR(36) NOT NULL,
  request_key VARCHAR(128) NOT NULL UNIQUE,
  retry_of CHAR(36) NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
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
  phase VARCHAR(32) NOT NULL DEFAULT 'timeline_scan',
  statistics JSON NULL,
  observations JSON NULL,
  markdown MEDIUMTEXT NULL,
  error VARCHAR(255) NULL,
  started_at DATETIME(6) NULL,
  ended_at DATETIME(6) NULL,
  accounting_reconciled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY historian_queue(status,available_at,created_at),
  KEY historian_versions(report_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS historian_events (
  run_id CHAR(36) NOT NULL,
  ordinal BIGINT NOT NULL,
  event_id VARCHAR(128) NOT NULL,
  occurred_at DATETIME(3) NOT NULL,
  player_uuid VARCHAR(36) NULL,
  payload JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  PRIMARY KEY(run_id,ordinal),
  UNIQUE KEY historian_event(run_id,event_id),
  KEY historian_player(run_id,player_uuid,occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS historian_receipts (
  run_id CHAR(36) NOT NULL,
  event_id VARCHAR(128) NOT NULL,
  PRIMARY KEY(run_id,event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS historian_calls (
  record_id VARCHAR(128) PRIMARY KEY,
  run_id CHAR(36) NOT NULL,
  phase VARCHAR(32) NOT NULL,
  task_key VARCHAR(191) NOT NULL,
  KEY historian_call_run(run_id,record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
