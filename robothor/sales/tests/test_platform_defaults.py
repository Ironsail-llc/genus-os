"""Sales defaults are the platform's, not one instance's locale or vendor.

Two defaults in ``SalesSettings`` were wrong in the same way — they described
the first instance rather than the platform:

* ``timezone = "America/Chicago"``. That string appears nowhere on
  ``origin/main``; the platform default is ``America/New_York`` from
  ``ROBOTHOR_TIMEZONE`` (``robothor/engine/config.py``, ``settings/model.py``).
  It is the operator's own locale copied into platform code — root CLAUDE.md
  rule 1 — and it silently disagreed with the scheduler every run.
* ``email_provider = "instantly"``. The subsystem's own documented posture is
  that every integration switch defaults off, and "off" for an email provider
  is ``"none"``. Defaulting to a named SaaS vendor contradicted it.
"""

from __future__ import annotations

import pytest

from robothor.constants import DEFAULT_TIMEZONE
from robothor.sales.models import SalesSettings
from robothor.sales.setup import sender_context_hash


def test_the_platform_timezone_default_is_the_platform_default():
    assert DEFAULT_TIMEZONE == "America/New_York"
    assert SalesSettings().timezone == DEFAULT_TIMEZONE


def test_the_instance_timezone_wins_over_the_platform_default(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_TIMEZONE", "Europe/Lisbon")

    assert SalesSettings().timezone == "Europe/Lisbon"


def test_no_operator_locale_is_hardcoded_in_the_sales_package():
    """The literal that started this: grep is the test."""
    from pathlib import Path

    package = Path(__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(package.parent))
        for path in package.rglob("*.py")
        if "tests" not in path.parts and "America/Chicago" in path.read_text()
    ]

    assert offenders == []


def test_every_integration_switch_defaults_off():
    settings = SalesSettings()

    assert settings.email_provider == "none"
    assert not settings.sending_enabled
    assert not settings.research_enabled
    assert not settings.enrichment_enabled
    assert not settings.promotion_enabled
    assert not settings.outcomes_enabled


def test_a_default_configuration_cannot_be_switched_to_sending():
    with pytest.raises(ValueError, match="email provider"):
        SalesSettings(sending_enabled=True)


def test_the_sender_context_hash_reads_the_same_defaults(monkeypatch):
    """A hash claiming a vendor the settings say is off is a lie about the context."""
    monkeypatch.setenv("ROBOTHOR_TIMEZONE", "Europe/Lisbon")
    bare = sender_context_hash({"senders": ["sender@example.com"]}, "sender@example.com")
    spelled = sender_context_hash(
        {
            "senders": ["sender@example.com"],
            "email_provider": "none",
            "timezone": "Europe/Lisbon",
        },
        "sender@example.com",
    )

    assert bare == spelled
