-- A manual selection during generation must not be overwritten by the in-flight run.
ALTER TABLE historian_reports ADD COLUMN display_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE historian_runs ADD COLUMN display_revision BIGINT NOT NULL DEFAULT 0;
