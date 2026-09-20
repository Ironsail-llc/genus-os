-- Extend encrypted observation metadata for explicitly selected linked documents.
ALTER TABLE autonomy_terms_snapshots DROP CONSTRAINT IF EXISTS autonomy_terms_snapshots_coverage_check;
ALTER TABLE autonomy_terms_snapshots ADD CONSTRAINT autonomy_terms_snapshots_coverage_check
    CHECK (coverage IN ('visible_text_only','visible_text_and_selected_documents'));
ALTER TABLE autonomy_terms_snapshots DROP CONSTRAINT IF EXISTS autonomy_terms_snapshots_document_count_check;
ALTER TABLE autonomy_terms_snapshots ADD CONSTRAINT autonomy_terms_snapshots_document_count_check
    CHECK (document_count BETWEEN 1 AND 26);
