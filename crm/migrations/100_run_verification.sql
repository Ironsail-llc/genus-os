-- Migration 100: run-level claim verification.
--
-- WHY. An agent's own claim is not the record. Run
-- 6cb7e492-f527-4992-b824-7110fb1cdf72 (agent main, trigger telegram) is
-- stored in this database with status='completed', output_text "✅ Payment
-- confirmed — $270 sent … via Venmo" (recipient elided), and a tool
-- trace consisting of exactly one write_file to /tmp. The CRM task stayed
-- TODO; no payment integration exists in this codebase. Nothing in the schema
-- could express "this run's claims were checked", so nothing did.
--
-- WHAT. Two columns on agent_runs carry the verdict computed by
-- robothor/engine/run_verification.py, and one ledger table records the
-- per-claim evidence behind it.
--
--   verified_status  one of run_verification.VERIFICATION_STATUSES, NULL
--                    while the ROBOTHOR_RUN_VERIFICATION_* flag is off.
--   verification     the Verdict.to_payload() document (claims, which tool
--                    steps supported each, why an unsupported one failed).
--
-- The CHECK is written to accept NULL so every historical row stays valid and
-- the migration is a no-op until the flag is enabled.

BEGIN;

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS verified_status text;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS verification jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agent_runs_verified_status_check'
    ) THEN
        ALTER TABLE agent_runs
            ADD CONSTRAINT agent_runs_verified_status_check
            CHECK (verified_status IS NULL OR verified_status IN (
                'no_claims', 'verified', 'unverified_claims', 'failed_verification'
            ));
    END IF;
END $$;

-- Partial: only flagged runs are ever queried by status, and the column is
-- NULL for every run predating the flag.
CREATE INDEX IF NOT EXISTS idx_agent_runs_verified_status
    ON agent_runs (verified_status)
    WHERE verified_status IS NOT NULL;

-- Evidence ledger: one row per claim/evidence pair, so "what was this run's
-- ✅ actually backed by" is a query rather than a re-derivation. Created here;
-- the writer lands in the follow-up PR, which owns the ingest path.
--
-- NO tenant_id column, deliberately. Migration 081 enables RLS on tables that
-- have one, but it is a one-shot DO block: a tenant_id added afterwards gets
-- no policy and would advertise an isolation this table does not have. Tenant
-- scope is inherited through run_id -> agent_runs (which IS RLS-protected),
-- and the FK cascade keeps the ledger from outliving its run.
CREATE TABLE IF NOT EXISTS agent_run_evidence (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_id     uuid REFERENCES agent_run_steps(id) ON DELETE SET NULL,
    kind        text NOT NULL,
    reference   text,
    verified    boolean NOT NULL DEFAULT false,
    detail      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_run_evidence_run
    ON agent_run_evidence (run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_run_evidence_unverified
    ON agent_run_evidence (kind, created_at DESC)
    WHERE NOT verified;

COMMENT ON COLUMN agent_runs.verified_status IS
    'Claim-verification verdict from robothor.engine.run_verification; NULL when the control was off.';
COMMENT ON COLUMN agent_runs.verification IS
    'Verdict.to_payload(): per-claim support, evidence step numbers, and why an unsupported claim failed.';
COMMENT ON TABLE agent_run_evidence IS
    'Per-claim evidence ledger for run verification. Tenant scope is inherited via run_id -> agent_runs.';

COMMIT;
