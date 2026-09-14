"""A degraded search result tells the model what to do next.

The operator's rule (2026-09-14): "you have an entire computer to use … you
should have just been able to log into your Chrome and use your Chrome as a
backup, and then tell me afterwards." The agent had a browser tool and leaned
on the dead search chain instead. The instruction now travels with the
degraded result itself, where the model reads it.
"""

from __future__ import annotations

from robothor.engine.tools.handlers import web


def test_a_degraded_result_with_weak_rows_carries_the_hint() -> None:
    out = web._degraded(
        [{"title": "t", "url": "https://example.com", "snippet": "s"}],
        "engines_unresponsive",
        "browser_parse_empty",
        ["duckduckgo: CAPTCHA"],
        "",
    )
    assert out["degraded"] == "engines_unresponsive"
    assert "browser tool" in out["hint"]
    assert "web_fetch" in out["hint"]


def test_a_failed_search_carries_the_hint_too() -> None:
    out = web._degraded([], "error", "browser_denied", [], "connection refused")
    assert "error" in out
    assert out["hint"] == web.SEARCH_DEGRADED_HINT


def test_the_hint_tells_the_agent_to_answer_first_and_report_after() -> None:
    lowered = web.SEARCH_DEGRADED_HINT.lower()
    assert "do not stop" in lowered
    assert "after you have the answer" in lowered
