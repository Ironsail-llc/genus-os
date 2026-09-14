"""Web search has an API provider.

``web_search`` prefers the Brave Search API when ``BRAVE_SEARCH_API_KEY`` is
set and otherwise scrapes public engines through SearXNG. Public engines
block automated clients (DuckDuckGo answers a CAPTCHA, Mojeek and Qwant
suspend the source), so an instance without a key has no working search and
nothing in the runtime says so — the 2026-09-14 outage was exactly that:
"unset" meant "no provider, no error", and the operator found out by asking
for a bakery and getting Wikipedia.

The check never prints the key. It reports presence, and ``--offline`` skips
nothing here because presence needs no network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok
from robothor.engine.search_config import SEARCH_KEY_ENV, search_api_configured

if TYPE_CHECKING:
    from robothor.doctor.context import DoctorContext


async def _provider(ctx: DoctorContext) -> Result:
    """Web search has an API provider, so results come from an index rather than a scrape.

    ``web_search`` prefers the Brave Search API when ``BRAVE_SEARCH_API_KEY``
    is set and otherwise scrapes public engines through SearXNG, which block
    automated clients. Without the key an instance has no working search and
    the runtime does not say so. Not repairable here: the key is a credential
    the operator obtains and stores.
    """
    if search_api_configured():
        return ok("Brave Search API key is set; web_search uses the API first")
    return fail(
        f"web_search has no API provider: {SEARCH_KEY_ENV} is not set, so every search "
        "scrapes public engines that block automated clients. Get a key at "
        "brave.com/search/api (free tier) and add it to the secrets store."
    )


CHECKS: tuple[Check, ...] = (
    Check(
        id="search.provider",
        title="Web search has an API provider",
        category="search",
        severity="recommended",
        run=_provider,
    ),
)
