"""Code the inner model writes during deep_reason cannot read another tenant.

deep_reason runs the RLM library's ``local`` environment: the inner model's
code is ``exec``'d inside the engine process, in the thread that called
``completion``, with ``__import__`` in its builtins. The tenant-bound REPL
helpers are therefore not the only door to memory: the code can import the
platform's memory or database modules itself and name any tenant it likes.

These tests run inner code the way that environment does -- the real
``LocalREPL`` when the ``rlms`` package is installed, a faithful stand-in
otherwise -- and check what it can reach.
"""

from __future__ import annotations

import builtins
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine import rlm_tool

TENANT = "test-tenant"
OTHER = "other-tenant"

_REPL_BLOCKED_BUILTINS = ("input", "eval", "exec", "compile", "globals", "locals")

#: A narrow subset of the library's restricted builtins: what these tests' cells
#: use, plus ``__import__``. Narrower than the library on purpose, so setup code
#: that leans on a builtin the library lacks (it has no ``frozenset``) fails
#: here too -- the library swallows a failing setup cell silently.
_REPL_BUILTINS = ("__import__", "print", "repr", "sorted", "ImportError", "Exception")


class _StandInREPL:
    """What rlms' ``LocalREPL`` does with code, without the package.

    One namespace, the custom tools as globals, restricted builtins that still
    include ``__import__`` and null out ``eval``/``exec``, the ``setup_code``
    environment kwarg run first (its failure swallowed, as the library does),
    then each cell ``exec``'d in the calling thread.
    """

    def __init__(self, custom_tools: dict[str, Any], setup_code: str | None = None, **_: Any):
        safe: dict[str, Any] = {name: getattr(builtins, name) for name in _REPL_BUILTINS}
        for name in _REPL_BLOCKED_BUILTINS:
            safe[name] = None
        self.ns: dict[str, Any] = {"__builtins__": safe, "__name__": "__main__"}
        for name, entry in custom_tools.items():
            self.ns[name] = entry["tool"] if isinstance(entry, dict) else entry
        if setup_code:
            self.run(setup_code)

    def run(self, code: str) -> str:
        try:
            exec(code, self.ns, self.ns)  # noqa: S102 - this IS the environment under test
        except Exception as error:  # noqa: BLE001 - surfaced as the REPL would
            return f"{type(error).__name__}: {error}"
        return repr(self.ns.get("result"))


class _RealREPL:
    """The installed library's own ``LocalREPL``."""

    def __init__(self, custom_tools: dict[str, Any], **env_kwargs: Any):
        from rlm.environments.local_repl import LocalREPL

        self.env = LocalREPL(custom_tools=custom_tools, **env_kwargs)

    def run(self, code: str) -> str:
        out = self.env.execute_code(code)
        return f"{out.locals.get('result')!r}\n{out.stderr}"


def _repl_kinds() -> list[Any]:
    kinds: list[Any] = [pytest.param(_StandInREPL, id="stand-in")]
    try:
        import rlm.environments.local_repl  # noqa: F401
    except ImportError:
        kinds.append(
            pytest.param(None, id="rlms", marks=pytest.mark.skip(reason="rlms not installed"))
        )
    else:
        kinds.append(pytest.param(_RealREPL, id="rlms"))
    return kinds


def _run_inner_code(repl_cls: Any, code: str, tmp_path: Any) -> str:
    """Run ``code`` as the inner model's one cell of a deep_reason session in TENANT."""

    class InProcessRLM:
        def __init__(self, *, custom_tools: dict[str, Any], **kwargs: Any) -> None:
            self._tools = custom_tools
            self._env_kwargs = dict(kwargs.get("environment_kwargs") or {})

        def completion(self, prompt: Any) -> SimpleNamespace:
            # The environment is built inside completion(), as the library does.
            repl = repl_cls(self._tools, **self._env_kwargs)
            return SimpleNamespace(response=repl.run(code), usage_summary=None)

    rlm_mod = MagicMock()
    rlm_mod.RLM = InProcessRLM
    logger_mod = MagicMock()
    logger_mod.RLMLogger.return_value.log_file_path = None
    with patch.dict("sys.modules", {"rlm": rlm_mod, "rlm.logger": logger_mod}):
        result = rlm_tool.execute_deep_reason(
            query="what does everyone drink",
            config=rlm_tool.DeepReasonConfig(tenant_id=TENANT, log_dir=str(tmp_path)),
        )
    assert "error" not in result, result
    return str(result["response"])


def _facts_by_tenant():
    """A memory whose rows belong to TENANT (Alice) and OTHER (Bob)."""
    rows = {
        TENANT: [{"fact_text": "Alice prefers tea", "category": "preference"}],
        OTHER: [{"fact_text": "Bob prefers coffee", "category": "preference"}],
    }
    asked: list[str] = []

    async def search_facts(query: str, limit: int = 10, tenant_id: str = "", **_: Any):
        asked.append(tenant_id)
        return rows.get(tenant_id, [])

    return search_facts, asked


@pytest.mark.parametrize("repl_cls", _repl_kinds())
def test_inner_code_importing_memory_cannot_read_another_tenant(repl_cls, tmp_path):
    search_facts, asked = _facts_by_tenant()
    code = (
        "import asyncio\n"
        "from robothor.memory.facts import search_facts\n"
        f"rows = asyncio.run(search_facts('drink', tenant_id={OTHER!r}))\n"
        "result = [r['fact_text'] for r in rows]\n"
    )
    with patch("robothor.memory.facts.search_facts", new=search_facts):
        response = _run_inner_code(repl_cls, code, tmp_path)

    assert "Bob" not in response
    assert OTHER not in asked


@pytest.mark.parametrize("repl_cls", _repl_kinds())
@pytest.mark.parametrize(
    "module",
    [
        "robothor.memory.facts",
        "robothor.db.connection",
        "robothor.crm.dal",
        "psycopg2",
        "redis",
        "importlib",
        "sys",
    ],
)
def test_inner_code_cannot_import_an_unbound_data_handle(repl_cls, module, tmp_path):
    """The platform's data layers, the drivers, and the one-step ways round the guard."""
    code = f"import {module}\nresult = 'imported'\n"
    response = _run_inner_code(repl_cls, code, tmp_path)

    assert "'imported'" not in response
    assert "ImportError" in response


@pytest.mark.parametrize("repl_cls", _repl_kinds())
def test_ordinary_analysis_imports_still_work(repl_cls, tmp_path):
    code = (
        "import json, re, math, collections, statistics, datetime\n"
        "result = json.dumps(sorted(collections.Counter('abca').items()))\n"
    )
    response = _run_inner_code(repl_cls, code, tmp_path)

    assert '[["a", 2], ["b", 1], ["c", 1]]' in response


@pytest.mark.parametrize("repl_cls", _repl_kinds())
def test_a_connection_inner_code_opens_is_bound_to_the_sessions_tenant(
    repl_cls, monkeypatch, tmp_path
):
    """Past the import guard, a database checkout still binds the session's tenant.

    The guard is not a sandbox: ``print.__self__`` is the real ``builtins``
    module. What a connection taken from the pool in the session's thread binds
    to for row-level security is ``effective_tenant()``, so it must be TENANT
    here, not the process's own tenant.
    """
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", OTHER)
    code = (
        "db = print.__self__.__import__('robothor.db.connection', fromlist=['x'])\n"
        "result = db.effective_tenant()\n"
    )
    response = _run_inner_code(repl_cls, code, tmp_path)

    assert repr(TENANT) in response
