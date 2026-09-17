-- Migration 125: which of a turn's tool calls ran at the same time.
--
-- WHY. Until now the engine executed one assistant message's tool calls
-- strictly one after another, so "the order of the step rows" and "the order
-- the model asked for them" were the same fact and neither needed recording.
-- With bounded parallel execution they are two facts, and only one of them is
-- reconstructable from the row order. Timestamps cannot answer it: two calls
-- that genuinely overlapped are indistinguishable from two that merely queued
-- behind one slow provider, which is exactly the "the control is on and does
-- nothing" shape this codebase keeps re-learning (2026-08-24, 2026-08-27).
--
-- WHAT. Two nullable columns on the append-only step trail:
--
--   batch_id        the id shared by every call of one turn that actually ran
--                   beside another. NULL on every historical row and on every
--                   turn that ran sequentially — a batch id stamped on single
--                   calls would make the ledger unable to answer the question
--                   the column exists for.
--   batch_position  the call's index in the MODEL's order within that turn,
--                   so "results came back in the order asked for" is checkable
--                   against the trail rather than asserted in a test.
--
-- Both are observability. Nothing reads them to make a decision.

BEGIN;

ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS batch_id uuid;
ALTER TABLE agent_run_steps ADD COLUMN IF NOT EXISTS batch_position integer NOT NULL DEFAULT 0;

COMMIT;
