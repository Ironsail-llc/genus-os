"""The shared Datadog read keys never reach anything an agent can run.

One key pair reads EVERY tenant's telemetry; the only separation between
tenants is the scope filter ``robothor/integrations/datadog/scope.py`` ANDs into
each query the platform sends. An agent holding the key itself skips that
filter with one ``curl``. So the two names are held back exactly like the
platform's own credentials: on every exec rung, from every grant, from hooks,
the browser driver and the Codex child, and from the C environment at startup
-- while the engine's own Datadog client can still read them.

Assertions bind to a local first, as in ``test_exec_env_platform_credentials``:
a failing operand here would print a child's environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from robothor.engine.exec_env import MODE_ENFORCE, MODE_OBSERVE, MODE_OFF, build_exec_env
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS

pytestmark = pytest.mark.timeout(120)

REPO_ROOT = Path(__file__).resolve().parents[3]
DATADOG_KEYS = ("ROBOTHOR_DATADOG_API_KEY", "ROBOTHOR_DATADOG_APP_KEY")
FIXTURE_VALUE = "fixture-not-a-real-credential"
RUNGS = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)
DUMP_NAMES = f'{sys.executable} -c "import os;print(chr(10).join(sorted(os.environ)))"'


def _boxlike() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GITHUB_TOKEN": FIXTURE_VALUE,
        **dict.fromkeys(DATADOG_KEYS, FIXTURE_VALUE),
    }


@pytest.mark.parametrize("name", DATADOG_KEYS)
def test_the_datadog_keys_are_classified_as_reaching_every_tenant(name):
    from robothor.secrets.classification import is_cross_tenant_credential, is_platform_credential

    cross = is_cross_tenant_credential(name)
    platform = is_platform_credential(name)
    assert cross
    assert platform


@pytest.mark.parametrize("name", DATADOG_KEYS)
def test_they_stay_application_secrets_resolved_vault_first(name):
    """Held back from children is not the same as environment-first."""
    from robothor.secrets.classification import is_bootstrap

    assert not is_bootstrap(name)


def test_the_names_come_from_the_integration():
    from robothor.integrations.datadog.client import API_KEY_ENV, APP_KEY_ENV
    from robothor.secrets.classification import CROSS_TENANT_CREDENTIALS

    assert {API_KEY_ENV, APP_KEY_ENV} <= CROSS_TENANT_CREDENTIALS


def test_the_scope_setting_is_not_a_credential():
    from robothor.secrets.classification import is_cross_tenant_credential

    assert not is_cross_tenant_credential("ROBOTHOR_DATADOG_SITE")
    assert not is_cross_tenant_credential("GITHUB_TOKEN")


@pytest.mark.parametrize("rung", RUNGS)
def test_no_rung_hands_a_datadog_key_to_a_child(rung):
    child = build_exec_env(agent_id="researcher", mode=rung, base=_boxlike()).env
    survivors = sorted(set(child) & set(DATADOG_KEYS))
    assert survivors == []


@pytest.mark.parametrize("rung", (MODE_OFF, MODE_OBSERVE))
def test_a_lower_rung_still_keeps_ordinary_credentials(rung):
    child = build_exec_env(agent_id="researcher", mode=rung, base=_boxlike()).env
    kept = child.get("GITHUB_TOKEN") == FIXTURE_VALUE
    assert kept


@pytest.mark.parametrize("name", DATADOG_KEYS)
def test_a_manifest_cannot_grant_a_datadog_key(name, monkeypatch):
    from robothor import secrets as secrets_module

    monkeypatch.setattr(secrets_module, "resolve_secret", _must_not_resolve)
    built = build_exec_env(agent_id="researcher", mode=MODE_ENFORCE, base={}, grants=(name,))
    granted = name in built.env
    assert not granted
    assert built.refused == (name,)


def _must_not_resolve(*_a, **_k):
    raise AssertionError("a refused grant must not be resolved")


@pytest.mark.asyncio
@pytest.mark.parametrize("rung", ("", MODE_OFF, MODE_OBSERVE, MODE_ENFORCE))
async def test_a_real_exec_child_sees_neither_key(rung, monkeypatch, tmp_path):
    import robothor.engine.exec_env as exec_env

    monkeypatch.setattr(exec_env, "grants_for_agent", lambda agent_id, workspace="": ())
    if rung:
        monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", rung)
    else:
        monkeypatch.delenv("ROBOTHOR_EXEC_ENV_MODE", raising=False)
    for name in DATADOG_KEYS:
        monkeypatch.setenv(name, FIXTURE_VALUE)
    result = await HANDLERS["exec"](
        {"command": DUMP_NAMES, "timeout": 30},
        ToolContext(agent_id="researcher", tenant_id="acme-child-a", workspace=str(tmp_path)),
    )
    seen = set(str(result.get("stdout", "")).split())
    survivors = sorted(seen & set(DATADOG_KEYS))
    assert "PATH" in seen, "the probe did not run"
    assert survivors == []


@pytest.mark.asyncio
async def test_a_shell_hook_gets_neither_key(monkeypatch, tmp_path):
    from robothor.engine import hook_registry as hr

    for name in DATADOG_KEYS:
        monkeypatch.setenv(name, FIXTURE_VALUE)
    out = tmp_path / "names.txt"
    hook = hr.LifecycleHook(
        event=hr.HookEvent.AGENT_START,
        handler_type="command",
        handler=f"{sys.executable} -c \"import os;open(r'{out}','w').write(chr(10).join(os.environ))\"",
    )
    context = hr.HookContext(event=hr.HookEvent.AGENT_START, agent_id="researcher", run_id="r1")
    await hr.HookRegistry()._run_command(hook, context)
    seen = set(out.read_text().split())
    survivors = sorted(seen & set(DATADOG_KEYS))
    assert "HOOK_EVENT" in seen, "the hook did not run"
    assert survivors == []


def test_the_browser_driver_filter_drops_them():
    """``filter_playwright_driver_env`` wraps the driver's env in this function."""
    from robothor.engine.exec_env import without_platform_credentials

    child = without_platform_credentials({**_boxlike(), "ACME_UNRELATED": "kept"})
    survivors = sorted(set(child) & set(DATADOG_KEYS))
    assert survivors == []
    assert child["ACME_UNRELATED"] == "kept"


def test_the_codex_child_gets_neither_key(monkeypatch):
    from robothor.engine.codex_provider import _codex_env

    for name in DATADOG_KEYS:
        monkeypatch.setenv(name, FIXTURE_VALUE)
    survivors = sorted(set(_codex_env()) & set(DATADOG_KEYS))
    assert survivors == []


# ── the startup drop, and the integration still working after it ────────────

#: Run in a process of its own: the drop changes the process's C environment.
_AFTER_THE_DROP = """
import json, os, subprocess, sys
source = sys.argv[1]
if source == "vault":
    from robothor import vault
    from robothor.constants import DEFAULT_TENANT
    def get(key, *, tenant_id=None, **_kw):
        if tenant_id != DEFAULT_TENANT or "datadog" not in key.lower():
            return None
        return "vault-" + ("api" if "api" in key.lower() else "app")
    vault.get = get
from robothor.engine.process_hardening import drop_inherited_credentials
dropped = drop_inherited_credentials()
show = [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"]
from robothor.integrations.datadog.client import client_from_settings, resolve_keys
api, app = resolve_keys()
client = client_from_settings()
print(json.dumps({
    "dropped": sorted(dropped),
    "child": json.loads(subprocess.check_output(show)),
    "shell": subprocess.check_output("env", shell=True, text=True).split(chr(10)),
    "keys": [api, app],
    "client": repr(client),
}))
"""


def _after_the_drop(source: str) -> dict:
    env = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")},
        "PYTHONPATH": str(REPO_ROOT),
        "ROBOTHOR_WORKSPACE": "/nonexistent",
    }
    if source == "env":
        env.update(dict.fromkeys(DATADOG_KEYS, FIXTURE_VALUE))
    done = subprocess.run(
        [sys.executable, "-c", _AFTER_THE_DROP, source],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    failed = done.returncode != 0
    assert not failed, "the probe process failed"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_drop_takes_them_from_children_and_the_client_still_builds():
    report = _after_the_drop("env")
    dropped = sorted(set(report["dropped"]) & set(DATADOG_KEYS))
    seen = set(report["child"]) | {line.split("=", 1)[0] for line in report["shell"]}
    survivors = sorted(seen & set(DATADOG_KEYS))
    keys_read = report["keys"] == [FIXTURE_VALUE, FIXTURE_VALUE]
    assert dropped == sorted(DATADOG_KEYS)
    assert survivors == []
    assert keys_read
    assert report["client"].startswith("DatadogClient(")


def test_a_vault_held_key_still_builds_a_client_after_the_drop():
    report = _after_the_drop("vault")
    keys_read = report["keys"] == ["vault-api", "vault-app"]
    assert keys_read
    assert report["client"].startswith("DatadogClient(")
