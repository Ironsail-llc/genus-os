"""``config_get`` / ``config_set`` — the assistant reads and changes its own settings (GEN-42).

Before these, the only route was ``exec`` + ``genus config set``. Both tools
go through the same ``robothor.settings.operator`` functions the CLI and the
bridge use, are restricted to the verified owner, never return a secret value,
and route ``apply=True`` through the authenticated host-exec path because the
engine itself cannot write ``/etc``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.identity import IdentityContext


@pytest.fixture(autouse=True)
def _top_level_run(monkeypatch):
    """Every call here is the owner's own top-level run unless a test says otherwise."""
    from types import SimpleNamespace

    from robothor.engine import session_registry

    monkeypatch.setattr(
        session_registry,
        "lookup",
        lambda run_id: SimpleNamespace(run=SimpleNamespace(parent_run_id=None)),
    )


if TYPE_CHECKING:
    from pathlib import Path

FAKE_PW = "hunter2-the-real-one-xyz"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """A workspace of our own, with none of the box's own configuration."""
    from robothor.settings import operator, reset_settings
    from robothor.settings.aliases import DEPRECATED_ALIASES, reset_alias_warnings
    from robothor.settings.sources import reset_unknown_key_warnings

    for name in list(os.environ):
        if name.startswith(("ROBOTHOR_", "GENUS_")) or name in DEPRECATED_ALIASES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(operator, "db_rows", lambda: {})
    reset_alias_warnings()
    reset_unknown_key_warnings()
    reset_settings()
    yield
    reset_settings()


def _owner_ctx(**overrides) -> ToolContext:
    identity = IdentityContext(
        tenant_id="test", channel="telegram", identifier="owner", verified=True, role="owner"
    )
    fields = {
        "agent_id": "main",
        "run_id": "run-test",
        "user_id": "owner",
        "tenant_id": "test",
        "identity": identity,
    }
    fields.update(overrides)
    return ToolContext(**fields)


def _call(name: str, args: dict, ctx: ToolContext | None = None) -> dict:
    from robothor.engine.tools.handlers.config_tools import HANDLERS

    return asyncio.run(HANDLERS[name](args, ctx or _owner_ctx()))


def _config_path(workspace: Path) -> Path:
    return workspace / ".robothor" / "config.yaml"


# ── registration ─────────────────────────────────────────────────────────────


class TestRegistration:
    def test_tools_registered(self):
        with patch("robothor.api.mcp.get_tool_definitions", return_value=[]):
            from robothor.engine.tools import ToolRegistry

            registry = ToolRegistry()
            for tool_name in ("config_get", "config_set"):
                assert tool_name in registry._schemas, f"{tool_name} not in registry"

    def test_handlers_dispatchable(self):
        from robothor.engine.tools.dispatch import builtin_handlers

        handlers = builtin_handlers()
        assert "config_get" in handlers
        assert "config_set" in handlers

    def test_readonly_classification(self):
        from robothor.engine.tools import READONLY_TOOLS

        assert "config_get" in READONLY_TOOLS
        assert "config_set" not in READONLY_TOOLS

    def test_config_tools_set(self):
        from robothor.engine.tools import CONFIG_TOOLS

        assert frozenset({"config_get", "config_set"}) == CONFIG_TOOLS

    def test_set_is_a_change_tool(self):
        from robothor.engine.act_observe import CHANGE, classify
        from robothor.engine.tools.constants import READONLY_TOOLS

        assert classify("config_set", {"key": "X", "value": "1"}, READONLY_TOOLS) == CHANGE
        assert classify("vault_set", {"key": "X", "value": "1"}, READONLY_TOOLS) == CHANGE

    def test_set_schema_has_apply_boolean(self):
        from robothor.engine.tools.schemas import get_engine_schemas

        params = get_engine_schemas()["config_set"]["function"]["parameters"]
        assert params["properties"]["apply"]["type"] == "boolean"
        assert set(params["required"]) == {"key", "value"}


# ── owner gate ───────────────────────────────────────────────────────────────


class TestOwnerOnly:
    @pytest.mark.parametrize("tool", ["config_get", "config_set"])
    def test_no_identity_is_denied(self, tool):
        out = _call(
            tool,
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "4"},
            ToolContext(agent_id="main", tenant_id="test"),
        )
        assert "error" in out and out.get("denied_by") == "owner_role"

    @pytest.mark.parametrize("tool", ["config_get", "config_set"])
    def test_a_member_is_denied(self, tool):
        ctx = _owner_ctx()
        member = IdentityContext(
            tenant_id="test", channel="telegram", identifier="m", verified=True, role="member"
        )
        out = _call(
            tool,
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "4"},
            replace(ctx, identity=member),
        )
        assert out.get("denied_by") == "owner_role"

    def test_an_unverified_owner_is_denied(self):
        ctx = _owner_ctx()
        unverified = IdentityContext(
            tenant_id="test", channel="vision", identifier="o", verified=False, role="owner"
        )
        out = _call(
            "config_get",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS"},
            replace(ctx, identity=unverified),
        )
        assert out.get("denied_by") == "owner_role"

    def test_a_benchmark_run_is_denied(self):
        out = _call(
            "config_get", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS"}, _owner_ctx(is_benchmark=True)
        )
        assert out.get("denied_by") == "owner_role"


# ── config_get ───────────────────────────────────────────────────────────────


class TestConfigGet:
    def test_reports_value_source_description_and_restart(self):
        out = _call("config_get", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS"})
        assert out["key"] == "ROBOTHOR_MAX_CONCURRENT_AGENTS"
        assert str(out["value"]) == "3"
        assert out["source"] == "default"
        assert out["description"]
        assert isinstance(out["restart_required"], bool)
        assert out["secret"] is False

    def test_reports_a_config_yaml_value(self, tmp_path):
        path = _config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("settings:\n  engine:\n    max_concurrent_agents: 7\n")
        out = _call("config_get", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS"})
        assert str(out["value"]) == "7"

    def test_a_secret_is_masked(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", FAKE_PW)
        out = _call("config_get", {"key": "ROBOTHOR_DB_PASSWORD"})
        assert FAKE_PW not in json.dumps(out)
        assert out["secret"] is True
        assert out["value"].startswith("<set")

    def test_unknown_key_is_an_error_with_suggestion(self):
        out = _call("config_get", {"key": "ROBOTHOR_MAX_CONCURENT_AGENTS"})
        assert "error" in out
        assert "ROBOTHOR_MAX_CONCURRENT_AGENTS" in out["error"]

    def test_prefix_lists_matching_settings(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", FAKE_PW)
        out = _call("config_get", {"prefix": "ROBOTHOR_DB"})
        assert out["count"] >= 1
        names = [s["key"] for s in out["settings"]]
        assert all(n.startswith("ROBOTHOR_DB") for n in names)
        assert "ROBOTHOR_DB_PASSWORD" in names
        assert FAKE_PW not in json.dumps(out)

    def test_neither_key_nor_prefix_is_an_error(self):
        assert "error" in _call("config_get", {})


# ── config_set ───────────────────────────────────────────────────────────────


class TestConfigSet:
    def test_writes_config_yaml_through_the_shared_writer(self, tmp_path):
        out = _call("config_set", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5"})
        assert out["success"] is True, out
        assert "max_concurrent_agents: 5" in _config_path(tmp_path).read_text()
        assert out["key"] == "ROBOTHOR_MAX_CONCURRENT_AGENTS"
        assert "pending_restart" in out

    def test_uses_the_operator_writer(self):
        from robothor.settings import operator

        with patch.object(operator, "apply_change", return_value=("robothor-engine",)) as apply:
            out = _call("config_set", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5"})
        assert apply.called
        assert out["pending_restart"] == ["robothor-engine"]

    def test_unknown_key_is_refused(self, tmp_path):
        out = _call("config_set", {"key": "ROBOTHOR_NOT_A_SETTING_AT_ALL", "value": "1"})
        assert "error" in out
        assert not _config_path(tmp_path).exists()

    def test_an_invalid_value_is_refused(self, tmp_path):
        out = _call("config_set", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "lots"})
        assert "error" in out
        assert not _config_path(tmp_path).exists()

    def test_a_secret_is_refused_and_never_echoed(self, tmp_path):
        out = _call("config_set", {"key": "ROBOTHOR_DB_PASSWORD", "value": FAKE_PW})
        assert "error" in out
        assert FAKE_PW not in json.dumps(out)
        assert "vault_set" in out["error"]
        assert not _config_path(tmp_path).exists()

    def test_a_control_character_is_refused(self, tmp_path):
        out = _call("config_set", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5\nX=1"})
        assert "error" in out
        assert not _config_path(tmp_path).exists()

    def test_an_environment_override_is_warned_about(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_AGENTS", "9")
        out = _call("config_set", {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5"})
        assert out["success"] is True
        assert "apply" in out.get("warning", "")


class TestConfigSetApply:
    def test_apply_without_host_exec_is_a_clear_error(self, tmp_path, monkeypatch):
        from robothor.engine import host_execution

        monkeypatch.setattr(host_execution, "socket_path", lambda: "")
        out = _call(
            "config_set",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5", "apply": True},
        )
        assert "error" in out
        assert "host exec" in out["error"]
        assert not _config_path(tmp_path).exists()

    def test_apply_when_not_eligible_is_a_clear_error(self, monkeypatch):
        from robothor.engine import host_execution

        monkeypatch.setattr(host_execution, "socket_path", lambda: "/run/x.sock")
        monkeypatch.setattr(host_execution, "eligible", lambda ctx: False)
        out = _call(
            "config_set",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5", "apply": True},
        )
        assert "host exec" in out["error"]

    def test_apply_routes_genus_config_set_through_host_exec(self, tmp_path, monkeypatch):
        from robothor.engine import host_execution

        seen: dict = {}

        async def fake_execute(ctx, command, timeout, env):
            seen["command"] = command
            return {
                "stdout": json.dumps(
                    {
                        "applied": True,
                        "pending_restart": ["robothor-engine"],
                        "errors": [],
                        "dropins": ["/etc/systemd/system/x.d/zz.conf"],
                        "restart_scheduled": ["genus-restart-1"],
                    }
                ),
                "stderr": "",
                "exit_code": 0,
                "execution_mode": "host_service",
            }

        monkeypatch.setattr(host_execution, "socket_path", lambda: "/run/x.sock")
        monkeypatch.setattr(host_execution, "eligible", lambda ctx: True)
        monkeypatch.setattr(host_execution, "execute", fake_execute)
        out = _call(
            "config_set",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5", "apply": True},
        )
        assert out["success"] is True, out
        assert out["applied"] is True
        assert out["restart_scheduled"] == ["genus-restart-1"]
        assert seen["command"] == (
            "genus config set --apply --json -- ROBOTHOR_MAX_CONCURRENT_AGENTS 5"
        )
        # The engine did not write anything itself; the host did.
        assert not _config_path(tmp_path).exists()

    def test_a_value_starting_with_a_dash_reaches_the_cli_as_a_value(self, monkeypatch):
        """``--`` ends the options, so argparse cannot read ``--fast`` as a flag."""
        import shlex

        from robothor.cli import _build_parser
        from robothor.engine import host_execution
        from robothor.settings import operator

        seen: dict = {}

        async def fake_execute(ctx, command, timeout, env):
            seen["command"] = command
            return {"stdout": json.dumps({"applied": True, "errors": []}), "exit_code": 0}

        monkeypatch.setattr(operator, "validate", lambda row, value: value)
        monkeypatch.setattr(host_execution, "socket_path", lambda: "/run/x.sock")
        monkeypatch.setattr(host_execution, "eligible", lambda ctx: True)
        monkeypatch.setattr(host_execution, "execute", fake_execute)
        out = _call(
            "config_set",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "--fast", "apply": True},
        )
        assert out["success"] is True, out
        argv = shlex.split(seen["command"])
        assert argv[0] == "genus"
        parsed = _build_parser().parse_args(argv[1:])
        assert parsed.name == "ROBOTHOR_MAX_CONCURRENT_AGENTS"
        assert parsed.value == "--fast"
        assert parsed.apply is True and parsed.json is True

    def test_apply_reports_host_errors(self, monkeypatch):
        from robothor.engine import host_execution

        async def fake_execute(ctx, command, timeout, env):
            return {
                "stdout": json.dumps({"applied": False, "pending_restart": [], "errors": ["nope"]}),
                "stderr": "",
                "exit_code": 1,
            }

        monkeypatch.setattr(host_execution, "socket_path", lambda: "/run/x.sock")
        monkeypatch.setattr(host_execution, "eligible", lambda ctx: True)
        monkeypatch.setattr(host_execution, "execute", fake_execute)
        out = _call(
            "config_set",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "5", "apply": True},
        )
        assert out["success"] is False
        assert out["errors"] == ["nope"]

    def test_apply_validates_before_calling_the_host(self, monkeypatch):
        from robothor.engine import host_execution

        called = SimpleNamespace(n=0)

        async def fake_execute(ctx, command, timeout, env):
            called.n += 1
            return {}

        monkeypatch.setattr(host_execution, "socket_path", lambda: "/run/x.sock")
        monkeypatch.setattr(host_execution, "eligible", lambda ctx: True)
        monkeypatch.setattr(host_execution, "execute", fake_execute)
        out = _call(
            "config_set",
            {"key": "ROBOTHOR_MAX_CONCURRENT_AGENTS", "value": "lots", "apply": True},
        )
        assert "error" in out
        assert called.n == 0
