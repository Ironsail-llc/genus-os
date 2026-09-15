"""Every shell a MODEL can compose goes through the same environment builder.

Review finding I5. The scrub was wired into ``exec`` and into ``Sandbox.exec``,
and three other paths ran ``subprocess.run(command, shell=True)`` with no
``env=`` at all — outside the ladder, invisible under ``observe``, and each one
driven by text a model produced:

* ``rlm_tool.deep_reason`` — the inner reasoning model composes the command.
  Held by ``main``, ``auto-agent``, ``auto-researcher`` and ``agent-architect``
  on the reference box.
* ``experiment_measure``'s metric and revert commands — read from an experiment
  definition, which an agent writes.
* ``thread_pool.run_accept`` — acceptance commands read out of a task body.

An agent denied ``exec`` but holding ``deep_reason`` had the whole credential
set back. So the guard at the bottom is the part that matters in a year: it
walks the source of every module under ``robothor/`` and fails on a
``subprocess`` call with ``shell=True`` and no ``env=``. A list of three call
sites would drift the moment somebody added a fourth.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ENGINE_ROOT.parent

FAKE = "ghp_FAKE0000aaaaaaaaaaaaaaaaaaaaaaaaaa"


#: A canary of our own, so the assertions never have to name — or print — a
#: credential this machine actually holds.
CANARY_NAME = "GENUS_SEC1_CANARY_TOKEN"


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
    monkeypatch.setenv(CANARY_NAME, FAKE)
    import robothor.engine.exec_env as exec_env

    monkeypatch.setattr(exec_env, "grants_for_agent", lambda agent_id, workspace="": ())


def _dump() -> str:
    import sys

    return (
        f'{sys.executable} -c "import os;'
        "print(chr(10).join(f'{k}={v}' for k,v in os.environ.items()))\""
    )


def _leaked(output: object) -> bool:
    """Whether the canary survived — as a BOOLEAN, bound to a plain local.

    Every caller assigns this to a local and asserts on the LOCAL. That is not
    style: pytest's assertion rewriting prints every intermediate value in a
    failing expression, so ``assert not _leaked(run(...))`` puts the child's
    entire environment — the real one, on whoever's machine ran the suite —
    into the test report. Which is the thing this file exists to prevent.
    """
    return FAKE in str(output)


def test_deep_reason_shell_does_not_inherit_the_credential(tmp_path):
    from robothor.engine.rlm_tool import _shell

    leaked = _leaked(_shell(_dump(), workspace=str(tmp_path)))
    assert not leaked, "deep_reason's shell inherited the engine's environment"


def test_the_experiment_metric_command_does_not_inherit_the_credential(tmp_path):
    from robothor.engine.tools.handlers.experiment import _run_metric_command

    leaked = _leaked(_run_metric_command(_dump(), workspace=str(tmp_path)))
    assert not leaked, "an experiment metric command inherited the engine's environment"


def test_the_acceptance_command_does_not_inherit_the_credential(tmp_path):
    from robothor.engine.thread_pool import _run_acceptance_command

    leaked = _leaked(_run_acceptance_command(_dump(), cwd=tmp_path, timeout=30).stdout)
    assert not leaked, "an acceptance command from a task body inherited the environment"


# ── the guard ────────────────────────────────────────────────────────────────


def _shell_calls_without_env(path: Path) -> list[str]:
    """Every ``subprocess`` call in ``path`` with ``shell=True`` and no ``env=``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = ""
        if isinstance(target, ast.Attribute):
            name = target.attr
            root = target.value
            if not (isinstance(root, ast.Name) and root.id == "subprocess"):
                continue
        else:
            continue
        if name not in {"run", "Popen", "check_output", "call", "check_call"}:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        shell = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if shell and "env" not in kwargs:
            found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return found


def test_no_shell_true_call_runs_with_the_inherited_environment():
    """A model can compose the command at every one of these call sites, so
    every one of them has to say what environment it hands over.

    ``env=`` is the assertion, not ``build_exec_env`` by name: a call site may
    legitimately pass a narrower environment it built another way. What is not
    legitimate is passing nothing, which means "everything".
    """
    offenders: list[str] = []
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if "tests" in relative.parts:
            continue
        offenders.extend(_shell_calls_without_env(path))
    assert not offenders, (
        "these shell-out call sites inherit the engine's whole environment — "
        f"pass env= from robothor.engine.exec_env.build_exec_env: {offenders}"
    )
