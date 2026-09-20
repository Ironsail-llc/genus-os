"""Construct root delegation state from the effective native agent manifest."""

import uuid

from robothor.engine.models import SpawnContext
from robothor.engine.spawn_limits import extend_limits


def make_spawn_context(agent_config, session, trace):
    return SpawnContext(
        # An untracked run (tracking_disabled) has no agent_runs row —
        # advertising its id would make every child's insert fail the
        # parent_run_id FK. Empty string → children record NULL parent.
        parent_run_id="" if session.run.tracking_disabled else session.run.id,
        parent_agent_id=agent_config.id,
        correlation_id=session.run.correlation_id or str(uuid.uuid4()),
        nesting_depth=0,
        max_nesting_depth=agent_config.max_nesting_depth,
        max_spawn_batch=agent_config.max_spawn_batch,
        allowed_agents=frozenset(agent_config.spawn_allowed_agents) or None,
        # `or None`: belt and braces against the empty string. A falsy-but-not-None
        # release id is not a release, and load_child_config must see None so the
        # child resolves from live manifests instead of staged_release_path(ws, "").
        fleet_release_id=agent_config.fleet_release_id or None,
        spawn_limits=extend_limits((), agent_config.max_spawn_total),
        remaining_token_budget=session.run.token_budget,
        parent_trace_id=trace.trace_id if trace else "",
        parent_span_id="",
        person_id=session.run.person_id,
        identity=getattr(session, "identity", None),
    )
