"""The credential outage must reach the operator.

2026-08-27: a single OpenRouter key hit its weekly cap. For 48 hours the
fleet emitted 949 x 403, 278 x "Key limit exceeded", 452 x "every
configured credential for it is retired", and 18 agents' worth of
"Primary model unreached" — and the operator was never paged about the
credential, because ``llm_client.py`` imports nothing from ``alerts`` and
every credential branch deliberately skips ``breaker.record_failure()``.

These tests pin the seam that closes that gap.
"""

from __future__ import annotations

import robothor.engine.provider_alerts as pa
from robothor.engine.key_pool import KeyPool, Retirement


def _capture(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(pa, "_in_pytest", lambda: False)
    monkeypatch.setattr(
        pa, "_deliver", lambda level, title, body: sent.append((level, title, body))
    )
    return sent


def test_provider_exhaustion_pages_at_critical(monkeypatch):
    sent = _capture(monkeypatch)
    pa.alert_provider_exhausted("OPENROUTER_API_KEY", Retirement.CREDIT_EXHAUSTED, pool_size=1)
    assert len(sent) == 1
    level, _title, _body = sent[0]
    assert level == "critical", (
        "a fleet-wide credential outage that only reaches the digest is the "
        "exact failure being fixed - alerts.py only pages on 'critical'"
    )


def test_the_page_names_the_env_var_and_never_the_key(monkeypatch):
    sent = _capture(monkeypatch)
    pa.alert_provider_exhausted("OPENROUTER_API_KEY", Retirement.AUTH_FAILED, pool_size=2)
    _level, title, body = sent[0]
    assert "OPENROUTER_API_KEY" in title + body
    assert "sk-" not in body


def test_the_page_states_the_consequence_not_just_the_fact(monkeypatch):
    """A page that says only 'a thing failed' reads as routine noise."""
    sent = _capture(monkeypatch)
    pa.alert_provider_exhausted("OPENROUTER_API_KEY", Retirement.CREDIT_EXHAUSTED, pool_size=1)
    _level, _title, body = sent[0]
    lowered = body.lower()
    assert "no model" in lowered or "every model" in lowered or "fleet" in lowered


def test_a_pool_of_one_says_so(monkeypatch):
    """The precondition for the outage must be named in the page itself."""
    sent = _capture(monkeypatch)
    pa.alert_provider_exhausted("OPENROUTER_API_KEY", Retirement.CREDIT_EXHAUSTED, pool_size=1)
    _level, _title, body = sent[0]
    assert "no spare" in body.lower() or "only key" in body.lower()


def test_a_periodic_cap_explains_that_a_top_up_will_not_fix_it(monkeypatch):
    sent = _capture(monkeypatch)
    pa.alert_provider_exhausted(
        "OPENROUTER_API_KEY", Retirement.QUOTA_EXHAUSTED_PERIODIC, pool_size=1
    )
    _level, _title, body = sent[0]
    assert "window" in body.lower() or "top-up" in body.lower() or "top up" in body.lower()


def test_nothing_is_sent_from_a_test_session(monkeypatch):
    sent: list = []
    monkeypatch.setattr(pa, "_deliver", lambda *a: sent.append(a))
    monkeypatch.setattr(pa, "_in_pytest", lambda: True)
    pa.alert_provider_exhausted("OPENROUTER_API_KEY", Retirement.CREDIT_EXHAUSTED, pool_size=1)
    assert sent == []


def test_a_delivery_failure_never_propagates(monkeypatch):
    def boom(*_a):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(pa, "_in_pytest", lambda: False)
    monkeypatch.setattr(pa, "_deliver", boom)
    pa.alert_provider_exhausted("OPENROUTER_API_KEY", Retirement.CREDIT_EXHAUSTED, pool_size=1)


def test_the_pool_hook_is_wired_end_to_end(monkeypatch):
    """A real KeyPool going exhausted must reach the alert function."""
    seen: list = []
    monkeypatch.setattr(pa, "_in_pytest", lambda: False)
    monkeypatch.setattr(pa, "_deliver", lambda level, title, body: seen.append(title))

    pool = KeyPool(
        ["sk-only"],
        on_exhausted=pa.exhaustion_hook("OPENROUTER_API_KEY", pool_size=1),
    )
    pool.retire("sk-only", Retirement.CREDIT_EXHAUSTED)

    assert len(seen) == 1
