-- External world-knowledge cache. Additive only: never shares memory_documents,
-- because knowledge is global, web-sourced, and TTL-scoped, while memory is a
-- person or conversation statement. Model proposals stay provisional until an
-- owner promotes them. Forgotten/retracted rows keep their version history.
CREATE TABLE IF NOT EXISTS knowledge_documents (
  id CHAR(36) NOT NULL PRIMARY KEY,
  canonical_key VARCHAR(160) NOT NULL,
  title VARCHAR(160) NOT NULL,
  claim VARCHAR(1500) NOT NULL,
  category VARCHAR(32) NOT NULL,
  ttl_hours INT UNSIGNED NOT NULL,
  valid_until DATETIME(6) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'provisional',
  current_version INT UNSIGNED NOT NULL,
  use_count INT UNSIGNED NOT NULL DEFAULT 0,
  content_hash CHAR(64) NOT NULL,
  source_url VARCHAR(500) NOT NULL,
  source_title VARCHAR(240) NOT NULL,
  source_excerpt VARCHAR(500) NOT NULL,
  fetched_at DATETIME(6) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  UNIQUE KEY knowledge_key (canonical_key),
  KEY knowledge_match (status, valid_until, category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS knowledge_versions (
  document_id CHAR(36) NOT NULL,
  version INT UNSIGNED NOT NULL,
  canonical_key VARCHAR(160) NOT NULL,
  title VARCHAR(160) NOT NULL,
  claim VARCHAR(1500) NOT NULL,
  category VARCHAR(32) NOT NULL,
  ttl_hours INT UNSIGNED NOT NULL,
  status VARCHAR(16) NOT NULL,
  content_hash CHAR(64) NOT NULL,
  source_url VARCHAR(500) NOT NULL,
  source_title VARCHAR(240) NOT NULL,
  source_excerpt VARCHAR(500) NOT NULL,
  operation VARCHAR(24) NOT NULL,
  reason VARCHAR(500) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  dsh_session_id VARCHAR(80) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (document_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
