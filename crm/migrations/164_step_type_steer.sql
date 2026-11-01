-- 164_step_type_steer.sql
-- Extend agent_run_steps.step_type CHECK to permit `steer`, the step a run
-- records when it takes an operator steer mid-flight (StepType.STEER in
-- robothor/engine/models.py), so the run steps API and the Runs drawer show it.
--
-- The list is the full StepType set plus `budget_stop`, which another pending
-- migration adds to the same constraint: whichever of the two applies last,
-- neither value is lost.

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
    'budget_stop'::text,
    'steer'::text
  ])) NOT VALID;

ALTER TABLE agent_run_steps
  VALIDATE CONSTRAINT agent_run_steps_step_type_check;
