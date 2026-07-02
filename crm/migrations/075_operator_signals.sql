-- Migration 075: operator signals for the goal-judge (self-improvement Phase 2).
--
-- The judge infers operator_satisfaction from the operator's words, but a real
-- verdict is stronger than an inference. Two tables capture real verdicts:
--
--   message_reactions  — 👍/👎/😡 the operator puts on a delivered message.
--                        Mapped to a -2..+2 verdict; linked to the run that
--                        produced the message when resolvable.
--   run_interventions  — the operator interrupting or steering a live run.
--                        An intervention is a strong "you're doing it wrong"
--                        signal that clamps the judge's satisfaction down.
--
-- goals.py / judge.py read these to anchor (clamp) the inferred satisfaction.

CREATE TABLE IF NOT EXISTS message_reactions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT 'robothor-primary',
    chat_id     TEXT NOT NULL,
    message_id  BIGINT NOT NULL,        -- telegram message id reacted to
    agent_id    TEXT,                   -- delivering agent, when resolvable
    run_id      UUID,                   -- run that produced the message, when resolvable
    emoji       TEXT,
    verdict     SMALLINT NOT NULL DEFAULT 0,  -- -2..+2 (reaction_to_verdict)
    reactor     TEXT,                   -- telegram user who reacted
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_message_reactions_agent
    ON message_reactions (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_reactions_run
    ON message_reactions (run_id)
    WHERE run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS run_interventions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT 'robothor-primary',
    run_id      UUID,
    agent_id    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('interrupt', 'steer')),
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_run_interventions_agent
    ON run_interventions (agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_interventions_run
    ON run_interventions (run_id)
    WHERE run_id IS NOT NULL;
