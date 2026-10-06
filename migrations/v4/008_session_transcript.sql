-- Filtered, queryable projection of the authoritative DSH Session log. Additive:
-- the official JSONL with attachments remains the durability and recovery source.
-- This table is rebuildable from it (INSERT IGNORE on session_event), never
-- rewritten by startup, and deliberately omits disposable payloads: assistant
-- raw streams, tool-result bodies, request tool schemas and inbox splices.
CREATE TABLE IF NOT EXISTS yunying_session_events (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  dsh_session_id VARCHAR(80) NOT NULL,
  event_seq BIGINT UNSIGNED NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  turn INT NULL,
  step INT NULL,
  role VARCHAR(16) NULL,
  content MEDIUMTEXT NULL,
  data JSON NULL,
  content_sha256 CHAR(64) NULL,
  truncated TINYINT(1) NOT NULL DEFAULT 0,
  event_time DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY session_event (dsh_session_id, event_seq),
  KEY session_type (dsh_session_id, event_type, event_seq)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
