"""``search.provider``: an instance without a search API key is told so by name.

The 2026-09-14 outage: no key, "unset" meant "no provider, no error", every
search scraped engines that block automated clients, nothing said why.
"""

from __future__ import annotations

import asyncio

from robothor.doctor.checks import search as search_checks
from robothor.doctor.registry import builtin_checks
from robothor.doctor.tests.conftest import make_ctx


def _run(check_id: str):
    check = next(c for c in search_checks.CHECKS if c.id == check_id)
    return asyncio.run(check.run(make_ctx()))


def test_a_missing_key_fails_by_name_and_says_where_to_get_one(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    result = _run("search.provider")
    assert result.status == "fail"
    assert "BRAVE_SEARCH_API_KEY" in result.detail
    assert "brave.com/search/api" in result.detail


def test_a_blank_key_counts_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "   ")
    assert _run("search.provider").status == "fail"


def test_a_set_key_passes_without_printing_it(monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSA-test-not-a-real-key-0000")
    result = _run("search.provider")
    assert result.status == "pass"
    assert "BSA-test" not in result.detail


def test_the_check_ships_in_the_doctor() -> None:
    ids = {c.id for c in builtin_checks()}
    assert "search.provider" in ids


def test_severity_is_recommended_so_a_fresh_install_gate_still_passes() -> None:
    check = next(c for c in search_checks.CHECKS if c.id == "search.provider")
    assert check.severity == "recommended"
