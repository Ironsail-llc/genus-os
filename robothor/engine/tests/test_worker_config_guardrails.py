"""A worker/drain run must keep the agent's guardrails.

`_build_worker_config` constructs a fresh AgentConfig for the worker (drain)
cycle, copying ~39 fields — but it silently dropped the security-relevant ones:
`guardrails`, `sandbox`, `exec_allowlist`, `write_path_allowlist`, and
`guardrails_opt_out`. A dataclass default filled the gap, so every worker run
executed with ZERO guardrail policies and `sandbox="local"`.

Found on 2026-07-13 by chasing an anomaly: main's :00 worker run kept tripping
sandbox_default after its manifest opted out via `sandbox: host`, while its
:03 heartbeat run (which uses the manifest config directly) did not. main is
the operator's primary agent and its worker fires every 2 hours, so its six
manifest policies — no_destructive_writes, no_sensitive_data, rate_limit,
desktop_safety, requires_human_task_closure, recurring_meeting_proposal_required
— had never applied to that run class.

The worker override exists to change *budget and warmup* for a drain cycle.
It must never quietly relax the security posture.
"""

from __future__ import annotations

from robothor.engine.config import AgentConfig, WorkerConfig
from robothor.engine.scheduler import _build_worker_config


def _agent() -> AgentConfig:
    return AgentConfig(
        id="a",
        name="A",
        description="d",
        guardrails=["no_destructive_writes", "rate_limit"],
        guardrails_opt_out=False,
        sandbox="host",
        exec_allowlist=["^git diff"],
        write_path_allowlist=["/tmp/ok"],
        worker=WorkerConfig(cron_expr="0 * * * *"),
    )


def test_worker_keeps_guardrail_policies():
    w = _build_worker_config(_agent())
    assert w.guardrails == ["no_destructive_writes", "rate_limit"], (
        "the worker override dropped the agent's guardrail policies — every "
        "drain run executed with no guardrails at all"
    )


def test_worker_keeps_sandbox_mode():
    w = _build_worker_config(_agent())
    assert w.sandbox == "host", (
        "the worker override reset sandbox to the default, discarding the "
        "agent's explicit opt-out (or opt-in)"
    )


def test_worker_keeps_exec_and_write_allowlists():
    w = _build_worker_config(_agent())
    assert w.exec_allowlist == ["^git diff"], "worker dropped exec_allowlist"
    assert w.write_path_allowlist == ["/tmp/ok"], "worker dropped write_path_allowlist"


def test_worker_keeps_guardrails_opt_out_flag():
    a = _agent()
    a.guardrails_opt_out = True
    assert _build_worker_config(a).guardrails_opt_out is True
