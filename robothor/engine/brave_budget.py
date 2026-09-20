"""Fund standard Brave web-search attempts at the current public Search rate.

This is conservative estimated usage, not an account invoice: credits are not
deducted and failed/unknown attempts retain their reservation. Answers and
custom enterprise contracts are outside this adapter's pricing contract.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from html.parser import HTMLParser
from typing import Any

import httpx

from robothor.engine.request_budget import RequestBudget, RequestBudgetError

PRICE_URL = "https://brave.com/search/api/"
_CACHE_SECONDS = 60
_cached: tuple[float, SearchPrice] | None = None


class _SearchSection(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hidden = 0
        self.heading: list[str] | None = None
        self.selected = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "h3" and not self.hidden:
            self.selected = False
            self.heading = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "h3" and self.heading is not None:
            self.selected = "".join(self.heading).strip() == "Search"
            self.heading = None

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.heading is not None:
            self.heading.append(data)
        elif self.selected:
            self.parts.append(data)


def parse_search_price(html: str) -> int:
    section = _SearchSection()
    section.feed(html)
    prices = re.findall(
        r"\$\s*(\d+(?:\.\d+)?)\s+per\s+1,?000\s+requests\b", " ".join(section.parts)
    )
    if len(prices) != 1:
        raise RequestBudgetError("Brave budget: standard Search price is missing or ambiguous")
    return int((Decimal(prices[0]) * 1000).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class SearchPrice:
    unit_units: int
    fetched_at: datetime
    document_hash: str


async def current_search_price() -> SearchPrice:
    global _cached
    now = time.monotonic()
    if _cached and 0 <= now - _cached[0] < _CACHE_SECONDS:
        return _cached[1]
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            async with client.stream("GET", PRICE_URL) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise RequestBudgetError("Brave budget: pricing document too large")
        price = SearchPrice(
            parse_search_price(body.decode("utf-8")),
            datetime.now(UTC),
            hashlib.sha256(body).hexdigest(),
        )
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise RequestBudgetError("Brave budget: current pricing unavailable") from exc
    _cached = (time.monotonic(), price)
    return price


class BraveSearchBudget:
    def __init__(self, budget: RequestBudget) -> None:
        self.budget = budget
        self.attempts: list[dict[str, Any]] = []

    async def prepare(self) -> None:
        await current_search_price()

    async def reserve(self) -> dict[str, Any]:
        price = await current_search_price()
        self.budget.reserve(price.unit_units)
        attempt = {
            "reserved_units": price.unit_units,
            "currency": "USD",
            "unit": "micro-USD",
            "price_source": PRICE_URL,
            "price_fetched_at": price.fetched_at.isoformat(),
            "price_document_sha256": price.document_hash,
            "response_status": None,
        }
        self.attempts.append(attempt)
        return attempt

    def receipt(self) -> dict[str, Any]:
        return {
            "basis": "standard_search_public_rate_without_credits",
            "invoice_verified": False,
            "reserved_units": sum(row["reserved_units"] for row in self.attempts),
            "attempts": [dict(row) for row in self.attempts],
        }
