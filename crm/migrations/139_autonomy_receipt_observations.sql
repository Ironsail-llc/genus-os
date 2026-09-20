-- Reuse the private encrypted observation archive for post-confirmation receipts.
ALTER TABLE autonomy_terms_snapshots DROP CONSTRAINT IF EXISTS autonomy_terms_snapshots_phase_check;
ALTER TABLE autonomy_terms_snapshots ADD CONSTRAINT autonomy_terms_snapshots_phase_check
    CHECK (phase IN ('before_input','before_submit','after_confirmation'));
