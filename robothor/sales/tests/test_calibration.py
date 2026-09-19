"""Frozen human assessments measure qualification without granting sales authority."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from robothor.db.connection import get_connection
from robothor.operations.store import Conflict
from robothor.sales.calibration import Calibration
from robothor.sales.models import Dossier
from robothor.sales.service import Sales
from robothor.sales.tests.test_service import researched


@pytest.fixture
def calibration(sales):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                (Path(__file__).parents[3] / "crm/migrations/131_sales_calibration.sql").read_text()
            )
        conn.commit()
    researched(sales)
    sales.configure({"active_policy_versions": {"network_access": "1"}}, "operator:test")
    return Calibration(sales)


def cohort(calibration, target=2):
    return calibration.create(
        name="Initial qualification review",
        target_size=target,
        agreement_target_percent=85,
        expected_settings_revision=calibration.sales.settings_snapshot()["revision"],
        actor="operator:reviewer",
        reason="Review the initial researched sample",
    )


def assess(calibration, item, decision="qualified", previous=None):
    return calibration.assess(
        item["id"],
        expected_snapshot_hash=item["snapshot_hash"],
        expected_assessment_id=previous,
        reference_decision=decision,
        actor="operator:reviewer",
        reason="Reviewed cited business evidence",
    )


def another(calibration, suffix, *, unknown=False, rejected=False):
    sales = calibration.sales
    original = next(p for p in sales.overview()["prospects"] if p["domain"] == "clinic.example.com")
    p = sales.discover(
        "Another clinic", f"https://{suffix}.example.com", "https://directory.example.com"
    )
    dossier = Dossier.model_validate(original["dossier"])
    if unknown:
        dossier.criteria = {}
    if rejected:
        dossier.evidence[0].value = False
    sales.research(p["id"], dossier, expected_version=0)
    sales.qualify(p["id"], "1")
    return p


def test_enrollment_is_bounded_concurrent_and_keeps_snapshot_after_research_changes(calibration):
    another(calibration, "second")
    another(calibration, "third")
    c = cohort(calibration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: calibration.enroll(c["id"], actor="operator:reviewer"), range(2)))
    items = calibration.items(c["id"])["items"]
    assert len(items) == 2 and [item["ordinal"] for item in items] == [1, 2]
    frozen = calibration.item(items[0]["id"])
    current = calibration.sales.get(frozen["prospect_id"])
    changed = Dossier.model_validate(current["dossier"])
    changed.summary = "Changed after enrollment"
    calibration.sales.research(current["id"], changed, expected_version=current["version"])
    assert calibration.item(items[0]["id"])["snapshot"] == frozen["snapshot"]
    assessment = assess(calibration, frozen)
    assert assessment["revision"] == 1
    assert calibration.sales.get(current["id"])["status"] == "researched"
    assert calibration.sales.ops.claim("sales.promote") is None


def test_corrections_preserve_original_agreement_and_require_current_assessment(calibration):
    c = cohort(calibration, 1)
    calibration.enroll(c["id"], actor="operator:reviewer")
    item = calibration.items(c["id"])["items"][0]
    first = assess(calibration, item, "rejected")
    with pytest.raises(Conflict, match="assessment"):
        assess(calibration, item, "qualified")
    second = assess(calibration, item, "qualified", first["id"])
    assert second["revision"] == 2 and second["supersedes"] == first["id"]
    report = calibration.report(c["id"])
    assert report["initial"]["agreement_percent"] == 0
    assert report["initial"]["false_positives"] == 1
    assert report["latest"]["agreement_percent"] == 100
    assert report["corrections"] == 1
    assert report["agreement_target_met"] is False
    assert len(calibration.item(item["id"])["assessments"]) == 2


def test_missing_and_uncertain_reviews_do_not_meet_target(calibration):
    another(calibration, "unknown", unknown=True)
    c = cohort(calibration)
    calibration.enroll(c["id"], actor="operator:reviewer")
    items = calibration.items(c["id"])["items"]
    assess(calibration, items[0])
    report = calibration.report(c["id"])
    assert report["initial"]["reviewed"] == 1 and not report["review_complete"]
    assess(calibration, items[1], "needs_research")
    report = calibration.report(c["id"])
    assert report["initial"]["agreement_percent"] == 100
    assert report["initial"]["uncertain_reference"] == 1
    assert not report["review_complete"] and not report["agreement_target_met"]
    assert report["by_buying_case"]["network_access"]["initial"]["reviewed"] == 2


def test_assessments_are_human_tenant_scoped_and_exact_snapshot_bound(calibration):
    c = cohort(calibration, 1)
    calibration.enroll(c["id"], actor="operator:reviewer")
    item = calibration.items(c["id"])["items"][0]
    other = Calibration(Sales("other-calibration-tenant"))
    assert other.list_cohorts()["items"] == []
    with pytest.raises(Conflict):
        other.item(item["id"])
    with pytest.raises(Conflict, match="snapshot"):
        assess(calibration, {**item, "snapshot_hash": "a" * 64})
    with pytest.raises(Conflict, match="Human"):
        calibration.enroll(c["id"], actor="agent:scout")
    assert calibration.item(item["id"])["assessments"] == []


def test_new_cohort_rejects_stale_settings_and_requires_approved_policies(calibration):
    with pytest.raises(Conflict, match="changed"):
        calibration.create(
            name="Stale review",
            target_size=100,
            agreement_target_percent=85,
            expected_settings_revision=0,
            actor="operator:reviewer",
            reason="Review the initial researched sample",
        )
    calibration.sales.configure({"active_policy_versions": {}}, "operator:test")
    with pytest.raises(Conflict, match="polic"):
        cohort(calibration)


def test_report_exposes_missed_fits_abstention_and_positive_yield(calibration):
    p = another(calibration, "unknown", unknown=True)
    another(calibration, "rejected", rejected=True)
    c = cohort(calibration, 3)
    calibration.enroll(c["id"], actor="operator:reviewer")
    items = calibration.items(c["id"])["items"]
    for item in items:
        assess(calibration, item, "qualified")
    report = calibration.report(c["id"])
    assert report["initial"]["reference_qualified"] == 3
    assert report["initial"]["missed_fits"] == 2
    assert report["initial"]["abstained_fits"] == 1
    assert report["initial"]["false_negatives"] == 1
    assert report["initial"]["qualified_precision_percent"] == 100
    assert report["review_complete"] and not report["agreement_target_met"]
    assert calibration.sales.get(p["id"])["status"] == "needs_research"


def test_pagination_and_correction_races_do_not_duplicate_assessments(calibration):
    c = cohort(calibration, 1)
    calibration.enroll(c["id"], actor="operator:reviewer")
    item = calibration.items(c["id"])["items"][0]
    assert calibration.items(c["id"], after=1)["items"] == []

    def submit(_):
        try:
            return assess(calibration, item)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(result is not None for result in results) == 1
    assert calibration.report(c["id"])["agreement_target_met"] is True


def test_full_sample_can_be_paged_without_losing_or_repeating_dossiers(calibration):
    calibration.sales.configure({"discovery_daily_limit": 100}, "operator:test")
    for number in range(22):
        another(calibration, f"page-{number}")
    c = cohort(calibration, 23)
    calibration.enroll(c["id"], actor="operator:reviewer")
    first = calibration.items(c["id"])
    assert len(first["items"]) == 20 and first["next_cursor"] == 20
    second = calibration.items(c["id"], after=first["next_cursor"])
    assert len(second["items"]) == 3 and second["next_cursor"] is None
    ids = [row["id"] for row in first["items"] + second["items"]]
    assert len(set(ids)) == 23
