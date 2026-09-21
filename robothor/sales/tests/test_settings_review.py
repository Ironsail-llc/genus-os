"""Reviewed operational settings cannot overwrite a newer operator decision."""

import pytest

from robothor.operations.store import Conflict
from robothor.sales.service import Sales


def test_reviewed_settings_preserve_bindings_and_reject_stale_changes(sales):
    sales.configure(
        {"agents": {"scout": "sample-scout"}, "monthly_limit_units": 100_000_000}, "operator:first"
    )
    reviewed = sales.settings_snapshot()
    sales.configure(
        {"monthly_limit_units": 500_000_000},
        "operator:second",
        expected_revision=reviewed["revision"],
        reason="Reviewed monthly pilot budget",
    )
    after = sales.settings_snapshot()
    assert after["revision"] == reviewed["revision"] + 1
    assert after["config"]["monthly_limit_units"] == 500_000_000
    assert after["config"]["agents"] == {"scout": "sample-scout"}
    assert after["config"]["sending_enabled"] is False
    with pytest.raises(Conflict, match="changed"):
        sales.configure(
            {"monthly_limit_units": 200_000_000},
            "operator:first",
            expected_revision=reviewed["revision"],
            reason="Stale pilot budget review",
        )
    assert sales.settings_snapshot() == after
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT actor,detail FROM operation_audit WHERE tenant_id=%s AND event='sales.configured' ORDER BY created_at DESC LIMIT 1",
            (sales.tenant,),
        )
        audit = cur.fetchone()
    assert audit["actor"] == "operator:second"
    assert audit["detail"]["reason"] == "Reviewed monthly pilot budget"
    assert audit["detail"]["revision"] == after["revision"]
    assert audit["detail"]["changes"]["monthly_limit_units"] == {
        "before": 100_000_000,
        "after": 500_000_000,
    }


def test_settings_snapshot_is_one_tenant_and_reports_unconfigured(sales):
    assert Sales("test-unconfigured-settings").settings_snapshot() == {"config": {}, "revision": 0}
    assert sales.settings_snapshot()["config"] == sales.settings()


@pytest.mark.parametrize("revision", [True, -1, "1"])
def test_invalid_review_revision_has_no_mutation(sales, revision):
    before = sales.settings()
    with pytest.raises(ValueError):
        sales.configure(
            {"discovery_daily_limit": 50},
            "operator:test",
            expected_revision=revision,
            reason="Reviewed daily pilot limit",
        )
    assert sales.settings() == before
