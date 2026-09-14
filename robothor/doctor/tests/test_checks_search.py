"""``search.provider``: an instance without a search API key is told so by name.

The 2026-09-14 outage: no key, "unset" meant "no provider, no error", every
search scraped engines that block automated clients, nothing said why. And
the first version of this check, run from an operator's shell on the box
that HAD the key in its runtime secrets file, said the key was missing — the
shell does not carry the services' EnvironmentFile. The check now looks
where the engine looks.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.doctor.checks import search as search_checks
from robothor.doctor.registry import builtin_checks
from robothor.doctor.tests.conftest import make_ctx


def _run(check_id: str = "search.provider"):
    check = next(c for c in search_checks.CHECKS if c.id == check_id)
    return asyncio.run(check.run(make_ctx()))


@pytest.fixture
def no_key(monkeypatch, tmp_path):
    """No key anywhere: shell env clean, an empty runtime root, vault says missing."""
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("ROBOTHOR_SECRETS_ROOT", str(tmp_path))
    monkeypatch.setattr("robothor.secrets.secret_source", lambda *_a, **_k: "missing")
    return tmp_path


def test_a_missing_key_fails_by_name_and_says_where_to_get_one(no_key) -> None:
    result = _run()
    assert result.status == "fail"
    assert "BRAVE_SEARCH_API_KEY" in result.detail
    assert "brave.com/search/api" in result.detail


def test_a_blank_key_counts_as_missing(no_key, monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "   ")
    assert _run().status == "fail"


def test_a_key_in_the_shell_environment_passes_without_printing_it(no_key, monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA-test-not-a-real-key-0000")
    result = _run()
    assert result.status == "pass"
    assert "BSA-test" not in result.detail


def test_a_key_in_the_runtime_secrets_file_passes_from_a_shell_that_lacks_it(no_key) -> None:
    """The services load /run/robothor/secrets.env as EnvironmentFile; an
    operator's shell does not. The doctor reads the file by name only."""
    run_dir = no_key / "run" / "robothor"
    run_dir.mkdir(parents=True)
    (run_dir / "secrets.env").write_text(
        'OPENROUTER_API_KEY="x"\nBRAVE_SEARCH_API_KEY="BSA-file-0000"\n'
    )
    result = _run()
    assert result.status == "pass"
    assert "BSA-file" not in result.detail
    assert "runtime secrets file" in result.detail


def test_a_runtime_file_without_the_key_is_not_a_pass(no_key) -> None:
    run_dir = no_key / "run" / "robothor"
    run_dir.mkdir(parents=True)
    (run_dir / "secrets.env").write_text('OPENROUTER_API_KEY="x"\n')
    assert _run().status == "fail"


def test_a_vault_only_key_is_reported_as_not_reaching_the_engine(no_key, monkeypatch) -> None:
    """Live 2026-09-14: the agent stored the key in the vault and search stayed dead."""
    monkeypatch.setattr("robothor.secrets.secret_source", lambda *_a, **_k: "vault")
    result = _run()
    assert result.status == "fail"
    assert "vault only" in result.detail
    assert "secrets store" in result.detail


def test_an_unreadable_runtime_file_says_to_rerun_with_privileges(no_key, monkeypatch) -> None:
    monkeypatch.setattr(search_checks, "_in_runtime_file", lambda _p: None)
    result = _run()
    assert result.status == "fail"
    assert "sudo" in result.detail


def test_the_check_ships_in_the_doctor() -> None:
    assert "search.provider" in {c.id for c in builtin_checks()}


def test_severity_is_recommended_so_a_fresh_install_gate_still_passes() -> None:
    check = next(c for c in search_checks.CHECKS if c.id == "search.provider")
    assert check.severity == "recommended"
