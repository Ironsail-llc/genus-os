"""The credentials that open the platform itself never reach a child process.

A rung of ``ROBOTHOR_EXEC_ENV_MODE`` decides what an agent's commands may keep
of the application credentials. It does not get a say over these: they open
every tenant's data at once, so they are held back on every rung, and they are
held back from every child the engine starts, not only the ones that go through
:func:`robothor.engine.exec_env.build_exec_env`.

Each assertion binds its answer to a local first. pytest prints the operands of
a failing expression, and an operand here would be a child's whole environment.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from robothor.engine.exec_env import MODE_ENFORCE, MODE_OBSERVE, MODE_OFF, build_exec_env
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers.filesystem import HANDLERS
from robothor.secrets.classification import bootstrap_names

pytestmark = pytest.mark.timeout(120)

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent.parent

FIXTURE_VALUE = "fixture-not-a-real-credential"

#: Beyond the declared bootstrap names: the ways a connection string arrives.
CONNECTION_NAMES = ("DATABASE_URL", "REDIS_URL", "PGPASSWORD", "PGPASSFILE")

PLATFORM_CREDENTIALS = tuple(sorted(bootstrap_names())) + CONNECTION_NAMES

RUNGS = (MODE_OFF, MODE_OBSERVE, MODE_ENFORCE)

DUMP_NAMES = f'{sys.executable} -c "import os;print(chr(10).join(sorted(os.environ)))"'


def _boxlike(**extra: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "ROBOTHOR_DB_HOST": "db.example.com",
        "GITHUB_TOKEN": FIXTURE_VALUE,
        **dict.fromkeys(PLATFORM_CREDENTIALS, FIXTURE_VALUE),
        **extra,
    }


@pytest.mark.parametrize("rung", RUNGS)
def test_no_rung_hands_a_platform_credential_to_a_child(rung):
    child = build_exec_env(agent_id="researcher", mode=rung, base=_boxlike()).env
    survivors = sorted(set(child) & set(PLATFORM_CREDENTIALS))
    assert survivors == []


@pytest.mark.parametrize("rung", (MODE_OFF, MODE_OBSERVE))
def test_a_lower_rung_still_leaves_everything_else_where_it_was(rung):
    """What the rungs are for: nothing an agent uses is taken without a choice."""
    child = build_exec_env(agent_id="researcher", mode=rung, base=_boxlike()).env
    kept = child.get("GITHUB_TOKEN") == FIXTURE_VALUE
    assert kept
    assert child["ROBOTHOR_DB_HOST"] == "db.example.com"


@pytest.mark.parametrize("name", PLATFORM_CREDENTIALS)
def test_every_platform_credential_is_classified(name):
    from robothor.secrets.classification import is_platform_credential

    assert is_platform_credential(name)


@pytest.mark.parametrize("name", ("GITHUB_TOKEN", "OPENAI_API_KEY", "ROBOTHOR_DB_HOST", "PATH"))
def test_an_ordinary_name_is_not_a_platform_credential(name):
    from robothor.secrets.classification import is_platform_credential

    assert not is_platform_credential(name)


def test_a_manifest_cannot_grant_a_connection_string(monkeypatch):
    built = build_exec_env(
        agent_id="researcher",
        mode=MODE_ENFORCE,
        base=_boxlike(),
        grants=("DATABASE_URL",),
    )
    granted = "DATABASE_URL" in built.env
    assert not granted
    assert built.refused == ("DATABASE_URL",)


@pytest.mark.asyncio
@pytest.mark.parametrize("rung", ("", MODE_OFF, MODE_OBSERVE, MODE_ENFORCE))
async def test_a_real_exec_child_sees_none_of_them(rung, monkeypatch, tmp_path):
    """Through the tool handler, on every rung, the unset default included."""
    import robothor.engine.exec_env as exec_env

    monkeypatch.setattr(exec_env, "grants_for_agent", lambda agent_id, workspace="": ())
    if rung:
        monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", rung)
    else:
        monkeypatch.delenv("ROBOTHOR_EXEC_ENV_MODE", raising=False)
    for name in PLATFORM_CREDENTIALS:
        # A *_FILE names a path the exec gates resolve, so it has to be one.
        fixture = str(tmp_path / "absent") if name.endswith("_FILE") else FIXTURE_VALUE
        monkeypatch.setenv(name, fixture)

    result = await HANDLERS["exec"](
        {"command": DUMP_NAMES, "timeout": 30},
        ToolContext(agent_id="researcher", workspace=str(tmp_path)),
    )
    seen = set(str(result.get("stdout", "")).split())
    survivors = sorted(seen & set(PLATFORM_CREDENTIALS))
    assert "PATH" in seen, "the probe did not run"
    assert survivors == []


# ── children that never pass through the builder ─────────────────────────────

#: Run in a process of its own: the call under test changes the process's
#: environment, which must not happen to the test runner.
_AFTER_THE_DROP = """
import asyncio, json, os, subprocess, sys
from robothor.engine.process_hardening import drop_inherited_credentials
names = sys.argv[1:]
dropped = drop_inherited_credentials()
show = [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"]
async def spawned():
    p = await asyncio.create_subprocess_exec(*show, stdout=subprocess.PIPE)
    out, _ = await p.communicate()
    return json.loads(out)
try:
    from playwright._impl import _transport
    driver = sorted(_transport.get_driver_env())
except ImportError:
    driver = None
print(json.dumps({
    "dropped": sorted(dropped),
    "driver": driver,
    "readable_here": all(os.environ.get(n) for n in names),
    "subprocess": json.loads(subprocess.check_output(show)),
    "shell": subprocess.check_output("env", shell=True, text=True).split(chr(10)),
    "asyncio": asyncio.run(spawned()),
}))
"""


def _run_drop(
    extra: dict[str, str] | None = None, strip: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """The drop, in a process of its own, with every platform credential set."""
    env = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "PYTHONPATH")},
        **dict.fromkeys(PLATFORM_CREDENTIALS, FIXTURE_VALUE),
        "PYTHONPATH": str(REPO_ROOT),
        # No workspace .env file fills a gap the test left on purpose.
        "ROBOTHOR_WORKSPACE": "/nonexistent",
        **(extra or {}),
    }
    # The process reads this itself and would choke on it as a fixture value.
    env.pop("ROBOTHOR_NATS_URL", None)
    for name in strip:
        env.pop(name, None)
    names = ["ROBOTHOR_DB_PASSWORD", "GENUS_AUTH_SIGNING_KEY"]
    return subprocess.run(
        [sys.executable, "-c", _AFTER_THE_DROP, *names],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _after_the_drop(**extra: str) -> dict:
    done = _run_drop(extra)
    failed = done.returncode != 0
    assert not failed, "the probe process failed"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_spawn_with_no_env_argument_inherits_none_of_them():
    report = _after_the_drop()
    seen = set(report["subprocess"]) | set(report["asyncio"])
    seen |= {line.split("=", 1)[0] for line in report["shell"]}
    survivors = sorted(seen & set(PLATFORM_CREDENTIALS))
    assert survivors == []


def test_the_process_itself_can_still_read_what_it_dropped():
    """Every platform read goes through ``os.environ``; those keep working."""
    report = _after_the_drop()
    assert report["readable_here"] is True
    assert "ROBOTHOR_DB_PASSWORD" in report["dropped"]


def test_the_daemon_startup_path_drops_them(monkeypatch):
    from robothor.engine import daemon, process_hardening

    called: list[str] = []
    monkeypatch.setattr(
        process_hardening, "drop_inherited_credentials", lambda: called.append("drop") or ()
    )
    monkeypatch.setattr(process_hardening, "harden_process", lambda: called.append("harden"))
    monkeypatch.setattr(daemon, "_log_pending_migrations", lambda: None)
    daemon._harden_and_state_posture()
    assert called == ["harden", "drop"]


@pytest.mark.asyncio
async def test_a_shell_hook_gets_no_platform_credential(monkeypatch, tmp_path):
    from robothor.engine import hook_registry as hr

    for name in PLATFORM_CREDENTIALS:
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
    survivors = sorted(seen & set(PLATFORM_CREDENTIALS))
    assert "HOOK_EVENT" in seen, "the hook did not run"
    assert survivors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rung", RUNGS)
async def test_a_shell_hook_keeps_its_own_credentials_on_every_rung(rung, monkeypatch, tmp_path):
    """A hook is operator-written, not model-composed: the exec ladder is not its
    policy. It loses the platform's credentials and nothing else, so a hook that
    posts to a webhook or authenticates to a policy service keeps working on
    ``enforce``."""
    from robothor.engine import hook_registry as hr

    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", rung)
    monkeypatch.setenv("ACME_WEBHOOK_URL", "https://hooks.example.com/fixture")
    monkeypatch.setenv("ACME_POLICY_TOKEN", FIXTURE_VALUE)
    for name in PLATFORM_CREDENTIALS:
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
    kept = sorted({"ACME_WEBHOOK_URL", "ACME_POLICY_TOKEN"} & seen)
    survivors = sorted(seen & set(PLATFORM_CREDENTIALS))
    assert kept == ["ACME_POLICY_TOKEN", "ACME_WEBHOOK_URL"]
    assert survivors == []


# ── a credential held in a connection setting's value ────────────────────────

DSN_IN_A_HOST = "postgresql://alice:hunter2@example.com/genus"
REDIS_DSN = "redis://alice:hunter2@example.com:6379/0"


def test_a_connection_setting_holding_a_credential_is_a_platform_credential():
    from robothor.engine.exec_env import without_platform_credentials

    child = without_platform_credentials(
        {
            "ROBOTHOR_DB_HOST": DSN_IN_A_HOST,
            "ROBOTHOR_REDIS_HOST": REDIS_DSN,
            "ROBOTHOR_DB_NAME": "genus",
            "ROBOTHOR_REDIS_PORT": "6379",
        }
    )
    assert sorted(child) == ["ROBOTHOR_DB_NAME", "ROBOTHOR_REDIS_PORT"]


@pytest.mark.parametrize("rung", RUNGS)
def test_no_rung_keeps_a_credential_in_a_connection_setting(rung):
    base = _boxlike(ROBOTHOR_DB_HOST=DSN_IN_A_HOST, ROBOTHOR_REDIS_HOST=REDIS_DSN)
    child = build_exec_env(agent_id="researcher", mode=rung, base=base).env
    survivors = sorted({"ROBOTHOR_DB_HOST", "ROBOTHOR_REDIS_HOST"} & set(child))
    assert survivors == []


def test_the_drop_takes_a_credential_out_of_a_connection_setting():
    report = _after_the_drop(ROBOTHOR_DB_HOST=DSN_IN_A_HOST)
    survivors = "ROBOTHOR_DB_HOST" in set(report["subprocess"])
    assert not survivors


# ── libpq's own password variables ───────────────────────────────────────────


@pytest.mark.parametrize("name", ("PGPASSWORD", "PGPASSFILE"))
def test_startup_refuses_a_password_only_libpq_can_see(name):
    """libpq reads these from the C environment the drop empties. An engine that
    authenticated through one alone would boot and then fail every connection,
    so it refuses to start and says which setting to use instead."""
    other = "PGPASSFILE" if name == "PGPASSWORD" else "PGPASSWORD"
    done = _run_drop({name: FIXTURE_VALUE}, strip=("ROBOTHOR_DB_PASSWORD", other))
    refused = done.returncode != 0
    assert refused
    assert name in done.stderr
    assert "ROBOTHOR_DB_PASSWORD" in done.stderr


def test_startup_accepts_a_libpq_password_alongside_the_platform_one():
    done = _run_drop({"PGPASSWORD": FIXTURE_VALUE, "ROBOTHOR_DB_PASSWORD": FIXTURE_VALUE})
    started = done.returncode == 0
    assert started


# ── the Playwright driver ────────────────────────────────────────────────────


@pytest.fixture
def driver_env(monkeypatch):
    """Playwright's own driver-environment builder, unwrapped for the test."""
    transport = pytest.importorskip("playwright._impl._transport")
    from playwright._impl import _driver

    monkeypatch.setattr(transport, "get_driver_env", _driver.get_driver_env)
    for name in PLATFORM_CREDENTIALS:
        monkeypatch.setenv(name, FIXTURE_VALUE)
    monkeypatch.setenv("ACME_UNRELATED", "kept")
    return transport


def _driver_survivors(transport) -> tuple[list[str], bool]:
    env = transport.get_driver_env()
    return sorted(set(env) & set(PLATFORM_CREDENTIALS)), env.get("ACME_UNRELATED") == "kept"


@pytest.mark.asyncio
async def test_the_browser_tool_starts_its_driver_without_them(driver_env, monkeypatch):
    """Playwright's driver copies ``os.environ`` for itself; the browser tool's
    one long-lived driver would hold every platform credential."""
    import playwright.async_api as api

    from robothor.engine.tools.handlers import browser

    seen: dict[str, object] = {}

    class _Fake:
        async def start(self):
            seen["survivors"], seen["kept"] = _driver_survivors(driver_env)
            return object()

    monkeypatch.setattr(api, "async_playwright", lambda: _Fake())
    monkeypatch.setattr(browser, "_playwright_instance", None)
    await browser._get_playwright()
    assert seen["survivors"] == []
    assert seen["kept"] is True


@pytest.mark.asyncio
async def test_the_render_tool_starts_its_driver_without_them(driver_env, monkeypatch):
    from robothor.engine.tools.handlers import web_render

    class _StopError(Exception):
        pass

    class _Network:
        async def get(self, url):
            raise _StopError

    monkeypatch.setattr(web_render, "RenderNetwork", _Network)
    with pytest.raises(_StopError):
        await web_render.render("https://example.com/")
    survivors, kept = _driver_survivors(driver_env)
    assert survivors == []
    assert kept


def test_the_startup_drop_covers_every_driver_this_process_starts():
    report = _after_the_drop()
    if report["driver"] is None:
        pytest.skip("playwright is not installed")
    survivors = sorted(set(report["driver"]) & set(PLATFORM_CREDENTIALS))
    assert survivors == []


@pytest.mark.asyncio
async def test_a_real_driver_process_is_spawned_without_them(driver_env, monkeypatch):
    """The environment handed to the driver's actual spawn, not our wrapper's."""
    from playwright.async_api import async_playwright

    from robothor.engine.process_hardening import filter_playwright_driver_env

    real_spawn = driver_env.asyncio.create_subprocess_exec
    spawned: list[dict[str, str]] = []

    async def spy(*args, **kwargs):
        spawned.append(dict(kwargs.get("env") or {}))
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(driver_env.asyncio, "create_subprocess_exec", spy)
    assert filter_playwright_driver_env()
    pw = await async_playwright().start()
    await pw.stop()
    assert len(spawned) == 1
    survivors = sorted(set(spawned[0]) & set(PLATFORM_CREDENTIALS))
    assert survivors == []
    assert spawned[0].get("ACME_UNRELATED") == "kept"


def test_the_codex_child_gets_no_platform_credential(monkeypatch):
    from robothor.engine.codex_provider import _codex_env

    for name in PLATFORM_CREDENTIALS:
        monkeypatch.setenv(name, FIXTURE_VALUE)
    survivors = sorted(set(_codex_env()) & set(PLATFORM_CREDENTIALS))
    assert survivors == []


def test_the_recovery_worker_receives_its_credentials_outside_its_environment(monkeypatch):
    """Its environment is readable while it starts; a pipe is not."""
    from robothor.engine import calendar_recovery_worker as worker

    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", FIXTURE_VALUE)
    payload = worker._credentials_for_child()
    carried = json.loads(payload)["ROBOTHOR_DB_PASSWORD"] == FIXTURE_VALUE
    assert carried

    code = (
        "import io,json,os,sys;"
        "from robothor.engine import calendar_recovery_worker as w;"
        "w._receive_credentials();"
        "print(json.dumps(os.environ.get('ROBOTHOR_DB_PASSWORD')));"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        input=payload,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO_ROOT), "HOME": "/nonexistent"},
        capture_output=True,
        timeout=60,
    )
    received = done.stdout.strip().endswith(json.dumps(FIXTURE_VALUE).encode())
    assert received


# ── the guard ────────────────────────────────────────────────────────────────

#: A module that copies the process's environment on purpose, and why that
#: cannot widen what a model reaches.
ALLOWED_COPIES = {
    # The builder and the allowlists: a copy is their input, never their output.
    "robothor/engine/exec_env.py": "the builder",
    "robothor/engine/coding/env.py": "allowlists built from nothing",
    "robothor/engine/tools/handlers/browser.py": "passes it to the builder as base",
    "robothor/engine/tools/handlers/desktop.py": "passes it to the builder as base",
    "robothor/engine/host_execution.py": "passes it to the builder as base",
    # Runs in the host service, never in the engine, on an operator's deploy.
    "robothor/engine/local_deploy.py": "the host service's own test and migration steps",
    # A KNOWN EXCEPTION, not a safe one. The operator's approve button runs a
    # script at a fixed workspace PATH, whose contents an agent that can write
    # the workspace can replace. The script is instance code and reads the
    # database from its environment, so the platform cannot pipe it the
    # credentials without changing it. Named in docs/configuration.md.
    "robothor/engine/telegram_handlers.py": "known exception: a workspace script, named in docs",
}


def _copies_the_process_environment(node: ast.AST) -> bool:
    def is_environ(value: ast.AST) -> bool:
        return isinstance(value, ast.Attribute) and value.attr == "environ"

    if isinstance(node, ast.Dict):
        return any(
            key is None and is_environ(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.Call):
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr == "copy":
            return is_environ(target.value)
        if isinstance(target, ast.Name) and target.id == "dict":
            return bool(node.args) and is_environ(node.args[0])
    return False


def test_no_engine_module_copies_the_process_environment_for_a_child():
    """An explicit copy brings back what the startup drop took out.

    ``os.environ`` keeps every value for the process's own reads, so a child
    handed a copy of it gets them all. A new copy goes through
    ``build_exec_env`` or ``without_platform_credentials``, or names its reason
    above.
    """
    found: list[str] = []
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT))
        if "/tests/" in relative or relative in ALLOWED_COPIES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not _copies_the_process_environment(node):
                continue
            found.append(f"{relative}:{getattr(node, 'lineno', 0)}")
    assert found == []


def test_every_allowed_copy_still_exists():
    """A stale entry is a hole nobody would notice reopening."""
    missing = [name for name in ALLOWED_COPIES if not (REPO_ROOT / name).is_file()]
    assert missing == []
