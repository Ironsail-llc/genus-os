"""Setup milestones recorded where runs are actually read.

Warmup happens before the first iteration, so a run that stalls there shows
nothing in `agent_run_steps` — only watchdog touch logs, which nobody reads
until something has already gone wrong.

Per-section granularity is the point: "warmup took 40s" says nothing,
"memory_blocks took 39 of them" says everything. That is what the fleet-wide
warmup-stall investigation needed and could not get.
"""

from __future__ import annotations

from types import SimpleNamespace

from robothor.engine.models import StepType
from robothor.engine.warmup_steps import SLOW_SECTION_SECONDS, record_warmup_steps


def _session():
    return SimpleNamespace(run=SimpleNamespace(id="run-1", steps=[]))


def _record(session, **kw):
    record_warmup_steps(
        session,
        prompt_ms=kw.pop("prompt_ms", 12),
        prompt_cached=kw.pop("prompt_cached", True),
        warmup_ms=kw.pop("warmup_ms", 340),
        warmup_kind=kw.pop("warmup_kind", "interactive"),
        warmup_chars=kw.pop("warmup_chars", 2048),
        section_timings=kw.pop("section_timings", {}),
    )


def _named(session):
    return {s.tool_name: s for s in session.run.steps}


# ── The two always-present milestones ─────────────────────────────────


def test_prompt_build_and_warmup_build_are_always_recorded():
    session = _session()
    _record(session)

    assert set(_named(session)) == {"system_prompt_build", "warmup_preamble_build"}


def test_every_step_is_a_warmup_phase_step():
    session = _session()
    _record(session, section_timings={"memory_blocks": 0.2})

    assert all(s.step_type is StepType.WARMUP_PHASE for s in session.run.steps)


def test_the_prompt_cache_outcome_is_recorded():
    session = _session()
    _record(session, prompt_cached=False)

    assert _named(session)["system_prompt_build"].tool_output["cached"] == "miss"


def test_an_absent_warmup_kind_reads_as_none_rather_than_empty():
    session = _session()
    _record(session, warmup_kind="")

    assert _named(session)["warmup_preamble_build"].tool_output["kind"] == "none"


# ── Section granularity ───────────────────────────────────────────────


def test_each_section_gets_its_own_step():
    session = _session()
    _record(session, section_timings={"memory_blocks": 39.0, "peers": 0.1})

    names = _named(session)
    assert "warmup_section:memory_blocks" in names
    assert "warmup_section:peers" in names


def test_section_timings_are_recorded_in_milliseconds():
    """The seconds-to-ms conversion is the kind of thing that silently drifts."""
    session = _session()
    _record(session, section_timings={"memory_blocks": 2.5})

    assert _named(session)["warmup_section:memory_blocks"].duration_ms == 2500


def test_a_slow_section_is_flagged():
    session = _session()
    _record(session, section_timings={"memory_blocks": SLOW_SECTION_SECONDS + 0.01})

    assert _named(session)["warmup_section:memory_blocks"].tool_output["slow"] is True


def test_a_fast_section_is_not():
    session = _session()
    _record(session, section_timings={"peers": 0.01})

    assert _named(session)["warmup_section:peers"].tool_output["slow"] is False


def test_no_section_timings_is_fine():
    session = _session()
    _record(session, section_timings=None)

    assert len(session.run.steps) == 2


# ── Robustness ────────────────────────────────────────────────────────


def test_a_section_name_is_carried_through_verbatim():
    """The name is how a stall is attributed. A plugin-contributed section must
    arrive intact rather than being normalised into something unrecognisable."""
    session = _session()

    _record(session, section_timings={"plugin:apollo_enrich": 1.2})

    step = _named(session)["warmup_section:plugin:apollo_enrich"]
    assert step.tool_output["section"] == "plugin:apollo_enrich"


def test_recording_never_raises():
    """None of this is worth failing a run over."""
    session = SimpleNamespace(run=SimpleNamespace(id="run-1", steps=None))
    _record(session)  # steps is None — append will raise internally
