-- Additive only. Conversation style is independent from long-term Memory:
-- Memory remains evidence, while only this validated profile can affect expression.
CREATE TABLE IF NOT EXISTS style_profiles (
  conversation_key VARCHAR(80) NOT NULL PRIMARY KEY,
  current_version INT UNSIGNED NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  rules_json JSON NOT NULL,
  candidates_json JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  updated_by VARCHAR(128) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  KEY style_status_updated (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS style_versions (
  conversation_key VARCHAR(80) NOT NULL,
  version INT UNSIGNED NOT NULL,
  rules_json JSON NOT NULL,
  candidates_json JSON NOT NULL,
  status VARCHAR(16) NOT NULL,
  content_hash CHAR(64) NOT NULL,
  operation VARCHAR(24) NOT NULL,
  reason VARCHAR(500) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  dsh_session_id VARCHAR(80) NULL,
  sources_json JSON NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (conversation_key, version),
  KEY style_version_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
