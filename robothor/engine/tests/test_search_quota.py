"""The Brave quota is known in advance, not discovered by failing.

Live, 2026-09-14: the free plan is 2,000 queries per 30 days at one per
second, and the fleet averages 50-60 searches a day with a 225-search day
this month — right at the cap. When the month runs out, every call would pay
three retries and fall to the scraper whose engines are IP-blocked. These
tests pin the behaviour that stops that: read the quota headers Brave sends
on every response, refuse to dial a dead month, and say so.
"""

from __future__ import annotations

from robothor.engine.search_quota import BraveQuota, parse_quota

# Exactly what api.search.brave.com returned on 2026-09-14 after ~35 calls.
_LIVE = {
    "x-ratelimit-limit": "1, 2000",
    "x-ratelimit-policy": "1;w=1, 2000;w=2592000",
    "x-ratelimit-remaining": "0, 1965",
    "x-ratelimit-reset": "1, 1398406",
}


def test_the_live_headers_parse_into_a_monthly_and_a_per_second_view() -> None:
    snap = parse_quota(_LIVE, now=1_000.0)

    assert snap is not None
    assert snap.month_limit == 2000
    assert snap.month_remaining == 1965
    assert snap.month_reset_at == 1_000.0 + 1398406
    assert snap.second_remaining == 0


def test_headers_without_the_pair_shape_are_ignored_not_fatal() -> None:
    assert parse_quota({}, now=0.0) is None
    assert parse_quota({"x-ratelimit-remaining": "garbage"}, now=0.0) is None
    # One number only: Brave's shape is "<second>, <month>"; a lone value is
    # not the monthly figure and must not be read as one.
    assert parse_quota({"x-ratelimit-limit": "2000", "x-ratelimit-remaining": "7"}, now=0.0) is None


def test_a_per_second_429_does_not_open_the_monthly_breaker() -> None:
    quota = BraveQuota()
    quota.record(_LIVE, status=429, now=1_000.0)

    assert quota.skip_reason(now=1_001.0) is None
    assert not quota.monthly_exhausted(now=1_001.0)


def test_a_429_with_no_monthly_budget_left_skips_brave_until_the_reset() -> None:
    quota = BraveQuota()
    headers = dict(_LIVE, **{"x-ratelimit-remaining": "0, 0", "x-ratelimit-reset": "1, 3600"})
    quota.record(headers, status=429, now=1_000.0)

    assert quota.monthly_exhausted(now=1_000.0)
    assert quota.skip_reason(now=1_000.0) == "brave_monthly_quota_exhausted"
    assert quota.skip_reason(now=1_000.0 + 3599) == "brave_monthly_quota_exhausted"
    # The month rolled over: dial Brave again rather than staying dark forever.
    assert quota.skip_reason(now=1_000.0 + 3601) is None


def test_a_successful_reply_after_exhaustion_closes_the_breaker() -> None:
    quota = BraveQuota()
    quota.record(dict(_LIVE, **{"x-ratelimit-remaining": "0, 0"}), status=429, now=1_000.0)
    assert quota.monthly_exhausted(now=1_000.0)

    quota.record(_LIVE, status=200, now=1_010.0)

    assert not quota.monthly_exhausted(now=1_010.0)


def test_low_water_is_a_tenth_of_the_month_by_default() -> None:
    quota = BraveQuota()
    quota.record(dict(_LIVE, **{"x-ratelimit-remaining": "1, 201"}), status=200, now=0.0)
    assert not quota.low_water()

    quota.record(dict(_LIVE, **{"x-ratelimit-remaining": "1, 200"}), status=200, now=0.0)
    assert quota.low_water()


def test_describe_is_operator_readable_and_never_carries_a_secret() -> None:
    quota = BraveQuota()
    quota.record(_LIVE, status=200, now=1_000.0)

    described = quota.describe(now=1_000.0)

    assert described == {
        "provider": "brave",
        "month_remaining": 1965,
        "month_limit": 2000,
        "resets_in_days": 16.2,
        "exhausted": False,
    }
    assert BraveQuota().describe(now=0.0) is None


def test_reset_forgets_everything() -> None:
    quota = BraveQuota()
    quota.record(dict(_LIVE, **{"x-ratelimit-remaining": "0, 0"}), status=429, now=1_000.0)
    quota.reset()

    assert quota.describe(now=1_000.0) is None
    assert quota.skip_reason(now=1_000.0) is None
