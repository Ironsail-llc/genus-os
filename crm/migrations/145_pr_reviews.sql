-- Migration 145: pr_reviews — the pr-reviewer suite's state (robothor/pr_review/store.py).
--
-- pr_reviews: one row per (tenant, repo, number). head_sha is the head we
-- last saw, last_reviewed_sha the head our last posted review covered,
-- queued_sha the head a CRM review task is open for. pending_trigger holds a
-- request the intake has not turned into a task yet (initial, new_head,
-- rereview, ambiguous). review_ids are OUR review ids on GitHub — the only
-- threads the reviewer ever resolves. last_review keeps the previous findings
-- (with their inline comment ids) for the next re-review. job_id is the
-- coding job pr_review_prepare started for queued_sha: pr_review_finalize
-- posts only that job's result, and only once (status reviewing -> posting
-- -> verdict, each step a compare-and-set). failed_at drives the retry
-- cooldown for a failed review on an unchanged head.
--
-- pr_review_messages: each Google Chat message the intake handled, once.
-- A message that failed to process is recorded with its error and skipped.
--
-- pr_review_cursors: where each Chat source's last poll stopped.
CREATE TABLE IF NOT EXISTS pr_reviews (
    tenant_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number > 0),
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'queued', 'reviewing', 'posting', 'changes_requested', 'commented',
        'approved', 'closed', 'failed', 'skipped'
    )),
    head_sha TEXT NOT NULL DEFAULT '',
    last_reviewed_sha TEXT NOT NULL DEFAULT '',
    queued_sha TEXT NOT NULL DEFAULT '',
    pending_trigger TEXT NOT NULL DEFAULT '',
    trigger_text TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    depth TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    chat_space TEXT NOT NULL DEFAULT '',
    chat_thread TEXT NOT NULL DEFAULT '',
    chat_message TEXT NOT NULL DEFAULT '',
    chat_poster TEXT NOT NULL DEFAULT '',
    telegram_ref TEXT NOT NULL DEFAULT '',
    followup BOOLEAN NOT NULL DEFAULT FALSE,
    review_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_review JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    failed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, repo, number)
);

CREATE INDEX IF NOT EXISTS pr_reviews_pending
    ON pr_reviews (tenant_id, updated_at) WHERE pending_trigger <> '';
CREATE INDEX IF NOT EXISTS pr_reviews_active
    ON pr_reviews (tenant_id) WHERE status IN ('queued', 'reviewing', 'posting');
CREATE INDEX IF NOT EXISTS pr_reviews_thread
    ON pr_reviews (tenant_id, chat_thread) WHERE chat_thread <> '';

CREATE TABLE IF NOT EXISTS pr_review_messages (
    tenant_id TEXT NOT NULL,
    message_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    failed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, message_name)
);

CREATE TABLE IF NOT EXISTS pr_review_cursors (
    tenant_id TEXT NOT NULL,
    source TEXT NOT NULL,
    cursor TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, source)
);

ALTER TABLE pr_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_reviews FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pr_reviews;
CREATE POLICY tenant_isolation ON pr_reviews
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE pr_review_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_review_messages FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pr_review_messages;
CREATE POLICY tenant_isolation ON pr_review_messages
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE pr_review_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_review_cursors FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pr_review_cursors;
CREATE POLICY tenant_isolation ON pr_review_cursors
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS pr_review_cursors;
--   DROP TABLE IF EXISTS pr_review_messages;
--   DROP TABLE IF EXISTS pr_reviews;
