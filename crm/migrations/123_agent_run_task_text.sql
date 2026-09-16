-- Migration 123: the wording a run was asked to satisfy.
--
-- WHY. `agent_runs` stores `user_prompt_chars` — a COUNT of the originating
-- prompt, not the prompt. The deliverable contract reads its requirement from
-- that wording ("save it to results/x.tsv", "use exactly the following
-- header"), and `run_finalizer` runs after the loop, by which point compaction
-- may have dropped the first user turn from the live message window: one
-- measured run made 333 requests against 3.4M input tokens and kept 62 of its
-- messages. So the contract had no source of task text at finalization and the
-- control could only ever fire on the runs that had a crm_task — a population
-- probed at ZERO explicit output paths in 4,000 rows over 60 days.
--
-- WHAT. One nullable text column carrying the originating prompt, capped in
-- the engine at 32 KB and passed through the same redactor as chat history
-- (`robothor.secrets.redaction.redact`) before it is written, so an operator
-- who pastes a credential into a task does not persist it here.
--
-- NULL on every historical row and on any run whose prompt was not retained;
-- the contract requires nothing when it is NULL, which is the safe direction.

BEGIN;

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS task_text text;

COMMIT;
