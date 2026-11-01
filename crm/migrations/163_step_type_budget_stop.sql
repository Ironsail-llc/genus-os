-- 163_step_type_budget_stop.sql
-- Extend agent_run_steps.step_type CHECK to permit budget_stop, the step every
-- budget, cap or stop now records when it refuses, ends or degrades a run (or,
-- in observe mode, would have). StepType in robothor/engine/models.py emits it;
-- without this the insert is rejected and the record never reaches the Runs
-- view, the same shape migration 062 fixed for warmup_phase.
--
-- NOT VALID + VALIDATE: every existing row already satisfies the wider set, and
-- validating separately takes a lock that does not block concurrent inserts.

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
    'warmup_phase'::text,
    'budget_stop'::text
  ])) NOT VALID;

ALTER TABLE agent_run_steps
  VALIDATE CONSTRAINT agent_run_steps_step_type_check;
