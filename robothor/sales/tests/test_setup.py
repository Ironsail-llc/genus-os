"""Operator setup edits configuration without storing credentials or enabling providers."""

import pytest

from robothor.operations.store import Conflict


def setup(sales):
    from robothor.sales.setup import Setup

    return Setup(
        sales,
        secret_names=lambda **kw: ["providers/instantly/api_key", "unrelated/private/key"],
        service_get=lambda name: None,
    )


def test_setup_readiness_lists_required_key_names_without_values_or_unrelated_keys(sales):
    snapshot = setup(sales).snapshot()
    assert snapshot["revision"] == sales.settings_snapshot()["revision"]
    instantly = next(item for item in snapshot["credentials"] if item["provider"] == "instantly")
    assert next(k for k in instantly["keys"] if k["name"].endswith("api_key"))["present"]
    assert not instantly["complete"]
    assert "unrelated/private/key" not in str(snapshot)
    assert snapshot["readiness"]["connected"] is None


def test_reviewed_setup_preserves_switches_and_binding_and_rejects_stale_edits(sales):
    service = setup(sales)
    before = service.snapshot()
    changes = {
        "senders": ["sales@example.com"],
        "postal_address": "TEST POSTAL ADDRESS",
        "unsubscribe_url": "https://example.com/unsubscribe",
        "business_sources": [
            {"source": "example", "account_id": "org-1", "refresh_seconds": 21600}
        ],
    }
    service.save(
        changes, before["revision"], "operator:test", "Reviewed sender and business account setup"
    )
    assert sales.settings()["senders"] == ["sales@example.com"]
    assert not sales.settings()["sending_enabled"]
    assert not sales.settings()["outcomes_enabled"]
    with pytest.raises(Conflict):
        service.save(
            changes, before["revision"], "operator:test", "Stale configuration must not overwrite"
        )
    for forbidden in (
        {"api_key": "test-secret"},
        {"sending_enabled": True},
        {"agents": {"draft": "other"}},
        {"active_policy_versions": {"anything": "x"}},
    ):
        with pytest.raises(ValueError):
            service.save(
                forbidden,
                sales.settings_snapshot()["revision"],
                "operator:test",
                "Must use the appropriate review surface",
            )


@pytest.mark.parametrize("expiry", ["2026-09-20T10:00:00", "not-a-date"])
def test_mailbox_review_dates_must_be_timezone_aware(sales, expiry):
    with pytest.raises(ValueError):
        sales.configure(
            {
                "senders": ["sales@example.com"],
                "mailbox_approved_until": {"sales@example.com": expiry},
            },
            "operator:test",
        )


def test_sender_setup_change_cancels_old_approvals_and_requests_provider_stop(sales):
    from robothor.sales.tests.test_guards import draft, prepared

    p = prepared(sales)
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    before = sales.settings_snapshot()
    setup(sales).save(
        {"postal_address": "UPDATED TEST POSTAL ADDRESS"},
        before["revision"],
        "operator:test",
        "Reviewed the updated business mailing address",
    )
    assert sales.ops.claim_action() is None
    assert sales.ops.claim("sales.stop")["payload"] == {"sender": "sales@example.com"}
