-- 062_step_type_warmup_phase.sql
-- 2026-05-04 — extend agent_run_steps.step_type CHECK to permit warmup_phase
-- and compaction values that StepType (robothor/engine/models.py) emits.
--
-- Background: commit 59f6ad9 instrumented warmup sections as warmup_phase
-- steps so the noon-storm investigation can see exactly which warmup
-- section completed before the run was cancelled. The CHECK constraint
-- was never updated, so every warmup_phase insert raises:
--   new row for relation "agent_run_steps" violates check constraint
--   "agent_run_steps_step_type_check"
-- The runner swallows the error (warmup steps are best-effort), but the
-- post-mortem queries that depend on these rows return nothing.
--
-- Fix: drop and recreate the constraint with the full StepType set.

ALTER TABLE agent_run_steps
  DROP CONSTRAINT IF EXISTS agent_run_steps_step_type_check;

ALTER TABLE agent_run_steps
  ADD CONSTRAINT agent_run_steps_step_type_check
  CHECK (step_type = ANY (ARRAY[
    'llm_call'::text,
    'tool_call'::text,
    'tool_result'::text,
    'error'::text,
    'planning'::text,
    'verification'::text,
    'checkpoint'::text,
    'scratchpad'::text,
    'escalation'::text,
    'guardrail'::text,
    'spawn_agent'::text,
    'plan_proposal'::text,
    'replan'::text,
    'error_recovery'::text,
    'deep_reason'::text,
    'compaction'::text,
    'warmup_phase'::text
  ]));
