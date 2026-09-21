"""Paid web search must reserve the current published rate before each attempt."""

from datetime import UTC, datetime

import pytest

from robothor.engine.brave_budget import BraveSearchBudget, SearchPrice, parse_search_price
from robothor.engine.request_budget import RequestBudget, RequestBudgetError


def test_parse_only_standard_search_price_not_credits_or_answer_tokens():
    html = "<h3>Search</h3><p><span>$5</span> per 1,000 requests</p><p>$5 monthly credit</p><h3>Answers</h3><p>$4 per 1,000 requests</p><p>$5 per million tokens</p>"
    assert parse_search_price(html) == 5000


@pytest.mark.parametrize(
    "html",
    [
        "$5 per 1,000 requests",
        "<h3>Answers</h3>$4 per 1,000 requests",
        "<h3>Search</h3>Contact us",
        "<h3>Search</h3>$5 per 1,000 requests $9 per 1,000 requests",
        "<h3>Search</h3><script>$5 per 1,000 requests</script>",
    ],
)
def test_missing_or_ambiguous_tariff_is_not_a_price(html):
    with pytest.raises(RequestBudgetError):
        parse_search_price(html)


@pytest.mark.asyncio
async def test_every_attempt_is_funded_and_unknown_charge_is_retained(monkeypatch):
    price = SearchPrice(5000, datetime.now(UTC), "a" * 64)

    async def quote():
        return price

    monkeypatch.setattr("robothor.engine.brave_budget.current_search_price", quote)
    budget = RequestBudget(10000)
    search = BraveSearchBudget(budget)
    attempt = await search.reserve()
    attempt["response_status"] = 429
    await search.reserve()  # unknown response, retained conservatively
    with pytest.raises(RequestBudgetError):
        await search.reserve()
    assert budget.charged_units == 10000
    assert search.receipt()["reserved_units"] == 10000
    assert len(search.receipt()["attempts"]) == 2


@pytest.mark.asyncio
async def test_unverified_pricing_cannot_reserve_or_spend(monkeypatch):
    async def unavailable():
        raise RequestBudgetError("Brave pricing unavailable")

    monkeypatch.setattr("robothor.engine.brave_budget.current_search_price", unavailable)
    budget = RequestBudget(10000)
    with pytest.raises(RequestBudgetError):
        await BraveSearchBudget(budget).reserve()
    assert budget.charged_units == 0


@pytest.mark.asyncio
async def test_public_price_fetch_is_unauthenticated_cached_and_expires_closed(monkeypatch):
    import httpx

    from robothor.engine import brave_budget

    calls = []
    clock = [100.0]

    def respond(request):
        calls.append(request)
        assert request.url == brave_budget.PRICE_URL
        assert "X-Subscription-Token" not in request.headers
        if len(calls) > 1:
            return httpx.Response(503)
        return httpx.Response(
            200, text="<h3>Search</h3><p>$5 per 1,000 requests</p><h3>Answers</h3>"
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(brave_budget, "_cached", None)
    monkeypatch.setattr(brave_budget.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        brave_budget.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    first = await brave_budget.current_search_price()
    assert first.unit_units == 5000
    assert first == await brave_budget.current_search_price()
    assert len(calls) == 1
    clock[0] += 61
    with pytest.raises(RequestBudgetError, match="pricing unavailable"):
        await brave_budget.current_search_price()
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allowance,statuses,expected_calls,expected_provider",
    [
        (4999, [200], 0, "searxng"),
        (5000, [200], 1, "brave"),
        (5000, [429, 200], 1, "searxng"),
        (10000, [429, 200], 2, "brave"),
        (5000, [None], 1, "searxng"),
    ],
)
async def test_actual_http_attempts_are_funded_and_receipted(
    monkeypatch, allowance, statuses, expected_calls, expected_provider
):
    import json
    from unittest.mock import AsyncMock

    import httpx

    from robothor.engine.request_budget import budget_scope
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import web

    monkeypatch.setattr(
        "robothor.engine.search_config.brave_search_key", lambda: "test-only-secret"
    )
    monkeypatch.setattr(
        "robothor.engine.brave_budget.current_search_price",
        AsyncMock(return_value=SearchPrice(5000, datetime.now(UTC), "a" * 64)),
    )
    monkeypatch.setattr(web.QUOTA, "skip_reason", lambda: None)
    monkeypatch.setattr(web.QUOTA, "record", lambda *args: None)
    monkeypatch.setattr(web.QUOTA, "monthly_exhausted", lambda: False)
    monkeypatch.setattr(web.QUOTA, "low_water", lambda: False)
    monkeypatch.setattr(
        web,
        "_scraped_search_with_fallback",
        AsyncMock(return_value={"provider": "searxng", "results": []}),
    )
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            # Assert at the HTTP boundary, before receiving a provider response.
            assert budget.charged_units == (len(calls) + 1) * 5000
            calls.append(url)
            status = statuses[len(calls) - 1]
            if status is None:
                raise httpx.ReadTimeout("unknown response")
            return httpx.Response(
                status,
                request=httpx.Request("GET", url),
                headers={"Retry-After": "0"},
                json={"web": {"results": [{"url": "https://example.com", "title": "Example"}]}},
            )

    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kwargs: Client())
    budget = RequestBudget(allowance)
    with budget_scope(budget):
        result = await web._search_with_fallback("example", 1, "auto", ToolContext())
    assert len(calls) == expected_calls
    assert result["provider"] == expected_provider
    assert budget.charged_units == expected_calls * 5000
    assert result["brave_budget"]["reserved_units"] == budget.charged_units
    assert [row["response_status"] for row in result["brave_budget"]["attempts"]] == statuses[
        :expected_calls
    ]
    assert "test-only-secret" not in json.dumps(result)
