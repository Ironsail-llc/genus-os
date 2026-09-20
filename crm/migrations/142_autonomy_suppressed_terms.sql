-- A suppressed observation is itself an observation: zero rows must never be
-- indistinguishable from the feature being switched off.
ALTER TABLE autonomy_terms_snapshots DROP CONSTRAINT IF EXISTS autonomy_terms_snapshots_coverage_check;
ALTER TABLE autonomy_terms_snapshots ADD CONSTRAINT autonomy_terms_snapshots_coverage_check
    CHECK (coverage IN ('visible_text_only','visible_text_and_selected_documents','suppressed_after_code'));
