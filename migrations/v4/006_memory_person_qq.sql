-- QQ-based cross-group person memory. Additive and retry-safe: only adds a column
-- and index, then backfills person_qq from the existing QQ identity bindings.
-- Group documents keep their conversation scope, while person documents become
-- readable across groups by every QQ of the same IGNG account.
SET @yunying_person_qq = IF((SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='memory_documents' AND COLUMN_NAME='person_qq') > 0, 'SELECT 1', 'ALTER TABLE memory_documents ADD COLUMN person_qq VARCHAR(20) NULL AFTER identity_id');
PREPARE yunying_stmt FROM @yunying_person_qq;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
SET @yunying_person_index = IF((SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='memory_documents' AND INDEX_NAME='memory_person_qq') > 0, 'SELECT 1', 'ALTER TABLE memory_documents ADD KEY memory_person_qq (person_qq,status)');
PREPARE yunying_stmt FROM @yunying_person_index;
EXECUTE yunying_stmt;
DEALLOCATE PREPARE yunying_stmt;
UPDATE memory_documents d JOIN memory_identity_bindings b ON b.identity_id=d.identity_id AND b.provider='qq' SET d.person_qq=b.external_id WHERE d.person_qq IS NULL AND d.identity_id IS NOT NULL;
