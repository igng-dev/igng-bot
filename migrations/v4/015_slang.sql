CREATE TABLE IF NOT EXISTS slang_entries (
  id VARCHAR(80) PRIMARY KEY,
  status VARCHAR(24) NOT NULL,
  current_version INT NOT NULL,
  payload JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  INDEX slang_status (status, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_versions (
  entry_id VARCHAR(80) NOT NULL,
  version INT NOT NULL,
  payload JSON NOT NULL,
  status VARCHAR(24) NOT NULL,
  actor VARCHAR(120) NOT NULL,
  reason VARCHAR(500) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (entry_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_meta (
  id INT PRIMARY KEY,
  generation BIGINT NOT NULL DEFAULT 0,
  policy JSON NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT IGNORE INTO slang_meta (id, policy) VALUES (1, '{"recognition":true,"recommendation":true,"maintenance":false,"auto_publish":false}');
CREATE TABLE IF NOT EXISTS slang_sources (
  id VARCHAR(80) PRIMARY KEY,
  config JSON NOT NULL,
  cursor_value VARCHAR(500) NULL,
  next_poll_at DATETIME(6) NULL,
  last_error VARCHAR(120) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_jobs (
  id CHAR(36) PRIMARY KEY,
  request_key CHAR(64) NOT NULL UNIQUE,
  source_id VARCHAR(80) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'queued',
  material JSON NOT NULL,
  config JSON NOT NULL,
  dsh_session_id CHAR(36) NULL,
  lease_token CHAR(36) NULL,
  lease_until DATETIME(6) NULL,
  attempts INT NOT NULL DEFAULT 0,
  submitted TINYINT NOT NULL DEFAULT 0,
  read_ids JSON NULL,
  error VARCHAR(120) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  INDEX slang_queue (status, created_at),
  INDEX slang_lease (status, lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_candidates (
  id CHAR(36) PRIMARY KEY,
  target_id VARCHAR(80) NOT NULL,
  job_id CHAR(36) NOT NULL,
  ordinal_value INT NOT NULL,
  payload JSON NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'candidate',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY slang_submission (job_id, ordinal_value)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_calls (
  job_id CHAR(36) NOT NULL,
  record_id VARCHAR(160) NOT NULL,
  tokens BIGINT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (job_id, record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_runs (
  dsh_session_id CHAR(36) PRIMARY KEY,
  job_id CHAR(36) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'active',
  config JSON NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_feedback (
  scope_key VARCHAR(80) NOT NULL,
  entry_id VARCHAR(80) NOT NULL,
  preference VARCHAR(24) NOT NULL,
  observations INT NOT NULL DEFAULT 0,
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (scope_key, entry_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_scopes (
  scope_key VARCHAR(80) PRIMARY KEY,
  enabled TINYINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS slang_observation_receipts (
  scope_key VARCHAR(80) NOT NULL,
  entry_id VARCHAR(80) NOT NULL,
  receipt_hash CHAR(64) NOT NULL,
  PRIMARY KEY (scope_key, entry_id, receipt_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
