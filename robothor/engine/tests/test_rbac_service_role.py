"""RBAC service-role scaffolding (Wave-1 hardening, PR-7, dark/additive).

System/cron/heartbeat runs have no interactive user_role, so check_tool_permission
early-returns allow — the whole autonomous fleet is unconstrained by RBAC. This PR
only adds the dormant scaffolding: a service_role on AgentConfig and a default
(service, *, allow) seed. Nothing enforces it yet (that's PR-8), so behavior is
unchanged: the service role allows every tool.
"""

from __future__ import annotations

import inspect


def test_agent_config_defaults_service_role():
    from robothor.engine.models import AgentConfig

    sig = inspect.signature(AgentConfig)
    assert "service_role" in sig.parameters
    assert sig.parameters["service_role"].default == "service"


def test_manifest_role_maps_to_service_role(tmp_path):
    """A manifest `role:` (or `service_role:`) populates AgentConfig.service_role."""
    from robothor.engine.config import manifest_to_agent_config

    base = {
        "id": "worker-x",
        "name": "Worker",
        "model": {"primary": "openrouter/test/model"},
        "schedule": {"cron": "0 * * * *"},
        "delivery": {"mode": "none"},
    }
    cfg = manifest_to_agent_config({**base, "role": "readonly-worker"})
    assert cfg.service_role == "readonly-worker"

    cfg_default = manifest_to_agent_config(base)
    assert cfg_default.service_role == "service"


def test_seed_includes_service_allow_all():
    """The default seed must allow-all for the service role (fleet unchanged)."""
    import robothor.engine.permissions as perms

    src = inspect.getsource(perms.seed_default_permissions)
    assert '("service", "*", "allow")' in src
    assert '("member", "*", "allow")' in src


def test_service_role_allows_all_tools(monkeypatch):
    """With (service, *, allow) rules, check_tool_permission allows any tool."""
    from robothor.engine import permissions

    # Simulate the seeded rule set returned from the DB.
    class _Cur:
        def execute(self, *a, **k):
            self._rows = [("*", "allow", "__default__")]

        def fetchall(self):
            return self._rows

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "robothor.db.connection.get_connection", lambda *a, **k: _Conn(), raising=False
    )
    assert permissions.check_tool_permission("service", "t1", "exec") is None
    assert permissions.check_tool_permission("service", "t1", "delete_task") is None
