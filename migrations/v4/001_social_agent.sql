-- Additive migration. Raw message_logs / V3 context_summaries are not rewritten.
CREATE TABLE IF NOT EXISTS yunying_sessions (
  conversation_key VARCHAR(80) NOT NULL PRIMARY KEY,
  platform VARCHAR(32) NOT NULL DEFAULT 'qq',
  conversation_type VARCHAR(16) NOT NULL,
  external_id VARCHAR(40) NOT NULL,
  dsh_session_id VARCHAR(80) NOT NULL UNIQUE,
  persistence_provider VARCHAR(40) NOT NULL DEFAULT 'official-jsonl',
  provisioning_status VARCHAR(16) NOT NULL DEFAULT 'provisioning',
  next_seq BIGINT UNSIGNED NOT NULL DEFAULT 1,
  social_state JSON NULL,
  paused TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  UNIQUE KEY platform_conversation (platform, conversation_type, external_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_ingress (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  event_id VARCHAR(96) NOT NULL UNIQUE,
  conversation_key VARCHAR(80) NOT NULL,
  raw_event JSON NOT NULL,
  prepared_event JSON NULL,
  delivery_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(255) NULL,
  available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  delivered_at DATETIME(6) NULL,
  KEY ingress_pending (delivery_status, available_at, id),
  KEY ingress_conversation (conversation_key, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_events (
  event_id VARCHAR(96) NOT NULL PRIMARY KEY,
  conversation_key VARCHAR(80) NOT NULL,
  seq BIGINT UNSIGNED NOT NULL,
  event_type VARCHAR(24) NOT NULL,
  message_id VARCHAR(64) NULL,
  payload JSON NOT NULL,
  dsh_message JSON NULL,
  delivered TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY conversation_seq (conversation_key, seq),
  KEY event_delivery (conversation_key, delivered, seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_sends (
  request_id VARCHAR(128) NOT NULL PRIMARY KEY,
  conversation_key VARCHAR(80) NOT NULL,
  payload_hash CHAR(64) NOT NULL,
  payload JSON NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  message_id VARCHAR(64) NULL,
  result JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  KEY sends_scope_time (conversation_key, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS yunying_ai_records (
  record_id VARCHAR(128) NOT NULL PRIMARY KEY,
  dsh_session_id VARCHAR(80) NOT NULL,
  request_seq BIGINT UNSIGNED NOT NULL,
  payload JSON NOT NULL,
  call_log_id BIGINT NULL,
  mirror_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY records_pending (mirror_status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_identities (
  id CHAR(36) NOT NULL PRIMARY KEY,
  display_name VARCHAR(160) NOT NULL DEFAULT '',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_identity_bindings (
  provider VARCHAR(32) NOT NULL,
  external_id VARCHAR(80) NOT NULL,
  identity_id CHAR(36) NOT NULL,
  verified_by VARCHAR(80) NOT NULL,
  shared_memory_opt_in TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (provider, external_id), KEY binding_identity (identity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_identity_audit (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  provider VARCHAR(32) NOT NULL,
  external_id VARCHAR(80) NOT NULL,
  identity_id CHAR(36) NOT NULL,
  operation VARCHAR(32) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  detail JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_documents (
  id CHAR(36) NOT NULL PRIMARY KEY,
  title VARCHAR(240) NOT NULL,
  markdown MEDIUMTEXT NOT NULL,
  document_type VARCHAR(24) NOT NULL,
  scope_key VARCHAR(80) NOT NULL,
  visibility VARCHAR(24) NOT NULL DEFAULT 'scope_private',
  identity_id CHAR(36) NULL,
  current_version INT UNSIGNED NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  KEY memory_scope (scope_key, visibility, status),
  KEY memory_person (identity_id, visibility, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_versions (
  document_id CHAR(36) NOT NULL,
  version INT UNSIGNED NOT NULL,
  title VARCHAR(240) NOT NULL,
  markdown MEDIUMTEXT NOT NULL,
  status VARCHAR(16) NOT NULL,
  content_hash CHAR(64) NOT NULL,
  operation VARCHAR(24) NOT NULL,
  reason VARCHAR(500) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  dsh_session_id VARCHAR(80) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (document_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_sources (
  document_id CHAR(36) NOT NULL,
  version INT UNSIGNED NOT NULL,
  event_id VARCHAR(96) NOT NULL,
  conversation_key VARCHAR(80) NOT NULL,
  message_id VARCHAR(64) NULL,
  identity_id CHAR(36) NULL,
  PRIMARY KEY (document_id, version, event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS memory_audit (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  document_id CHAR(36) NULL,
  version INT UNSIGNED NULL,
  operation VARCHAR(32) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  scope_key VARCHAR(80) NOT NULL,
  allowed TINYINT(1) NOT NULL,
  detail JSON NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  KEY audit_document (document_id, id), KEY audit_scope (scope_key, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
