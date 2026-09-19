"""Exact limits are shared without treating personal funds as company assets."""

from decimal import Decimal

import pytest


@pytest.mark.parametrize(
    "amount,used,reason",
    [
        (10000, 90000, "within_policy"),
        (10001, 90000, "monthly_limit"),
        (100001, 0, "per_transaction_limit"),
        (1, None, "usage_unavailable"),
        (1, -1, "usage_unavailable"),
    ],
)
def test_exact_integer_and_decimal_boundaries(amount, used, reason):
    from robothor.entity.spend_limits import decide_limits

    for convert in (int, Decimal):
        _, result = decide_limits(
            amount=convert(amount),
            per_transaction_limit=convert(100000),
            monthly_limit=convert(100000),
            monthly_used=convert(used) if used is not None else None,
        )
        assert result.value == reason


def test_optional_treasury_approval_and_daily_limit_do_not_apply_to_personal_grants():
    from robothor.entity.spend_limits import decide_limits

    args = {"amount": 100, "per_transaction_limit": 1000, "monthly_limit": 1000, "monthly_used": 0}
    assert decide_limits(**args)[0].value == "allow"
    assert decide_limits(**args, approval_threshold=100)[0].value == "approval_required"
    assert decide_limits(**args, daily_used=950, daily_limit=1000)[1].value == "daily_limit"
    assert decide_limits(**args, usage_matches=False)[1].value == "usage_snapshot_mismatch"


@pytest.mark.parametrize("value", [True, 1.0, -1, Decimal("NaN"), Decimal("Infinity")])
def test_invalid_arithmetic_never_authorizes_a_charge(value):
    from robothor.entity.spend_limits import within_limit

    assert not within_limit(value, 1, 100)
    assert not within_limit(0, value, 100)
    assert not within_limit(0, 1, value)
