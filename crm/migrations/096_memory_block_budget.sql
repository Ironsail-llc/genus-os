-- Migration 096: prune state + a manifest for the always-loaded memory tier
--
-- WHY
--   agent_memory_blocks.max_chars is written at seed and read by nothing.
--   Measured on the live table: 2,492 blocks, 7.1M characters (~1.78M tokens),
--   130 blocks over their own declared budget, the largest 57,051 characters
--   (~14k tokens) — and 2,092 never read even once.
--
--   Blocks are the ALWAYS-LOADED tier, so every character is paid on every
--   turn. A limit nobody checks is worse than no limit: it looks like a budget
--   and behaves like none.
--
-- WHY A SOFT DELETE
--   pruned_at rather than DELETE, so a block that turns out to matter is one
--   UPDATE away from returning. Membership in the always-loaded tier should be
--   a curated working set, not an accumulating log — but "curated" must not
--   mean "destroyed on a heuristic".
--
-- WHY ITS OWN MANIFEST
--   memory_facts_audit.fact_id is NOT NULL, so blocks cannot borrow it without
--   inventing fake fact ids. A prune with no record of which rows a run touched
--   is reversible in principle and irreversible in practice.
--
-- SAFETY
--   Purely additive. Nothing is pruned here; pruned_at is NULL for every
--   existing row and no read path filters on it yet.
--
-- IDEMPOTENT: ADD COLUMN / CREATE TABLE / CREATE INDEX IF NOT EXISTS.
-- Rollback:
--   ALTER TABLE agent_memory_blocks DROP COLUMN IF EXISTS pruned_at;
--   DROP TABLE IF EXISTS memory_block_prune_log;

BEGIN;

ALTER TABLE agent_memory_blocks
    ADD COLUMN IF NOT EXISTS pruned_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_blocks_pruned
    ON agent_memory_blocks(pruned_at)
    WHERE pruned_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_block_prune_log (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    block_id     BIGINT,
    block_name   TEXT NOT NULL,
    block_type   TEXT,
    content_chars INTEGER,
    write_count  INTEGER,
    reason       TEXT NOT NULL DEFAULT '',
    -- Full content, so a restore does not depend on the row surviving.
    content_snapshot TEXT,
    pruned_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_block_prune_log_pruned
    ON memory_block_prune_log(pruned_at DESC);

ALTER TABLE memory_block_prune_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_block_prune_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON memory_block_prune_log;
CREATE POLICY tenant_isolation ON memory_block_prune_log
    USING (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    )
    WITH CHECK (
        current_setting('app.tenant_id', true) IS NULL
        OR current_setting('app.tenant_id', true) = ''
        OR tenant_id = current_setting('app.tenant_id', true)
    );

COMMIT;
