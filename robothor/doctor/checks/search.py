"""Web search has an API provider.

``web_search`` prefers the Brave Search API when ``BRAVE_SEARCH_API_KEY`` is
in the engine's environment and otherwise scrapes public engines through
SearXNG. Public engines block automated clients (DuckDuckGo answers a
CAPTCHA, Mojeek and Qwant suspend the source), so an instance without a key
has no working search and nothing in the runtime says so — the 2026-09-14
outage was exactly that: "unset" meant "no provider, no error", and the
operator found out by asking for a bakery and getting Wikipedia.

Where the key is looked for, in order, mirrors how the engine actually gets
it: the process environment; the rendered runtime secrets file the services
load as their ``EnvironmentFile`` (an operator's shell does not have it, so
the doctor reads the file by name only); and finally the vault — which the
search tool does NOT read, so a vault-only key is reported as such rather
than as configured. The check never prints a value.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.doctor.model import Check, Result, fail, ok
from robothor.engine.search_config import SEARCH_KEY_ENV, search_api_configured

if TYPE_CHECKING:
    from robothor.doctor.context import DoctorContext

_MISSING = (
    f"web_search has no API provider: {SEARCH_KEY_ENV} is not set, so every search "
    "scrapes public engines that block automated clients. Get a key at "
    "brave.com/search/api (free tier) and add it to the secrets store."
)


def _runtime_secrets_file() -> Path:
    """The file the services load as EnvironmentFile (same seam as load-secrets.sh)."""
    root = os.environ.get("ROBOTHOR_SECRETS_ROOT", "").rstrip("/")
    return Path(f"{root}/run/robothor/secrets.env")


def _in_runtime_file(path: Path) -> bool | None:
    """True/False when the rendered file can be read; None when it exists but cannot."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return any(line.startswith(f"{SEARCH_KEY_ENV}=") for line in text.splitlines())


async def _provider(ctx: DoctorContext) -> Result:
    """Web search has an API provider, so results come from an index rather than a scrape.

    ``web_search`` prefers the Brave Search API when ``BRAVE_SEARCH_API_KEY``
    reaches the engine's environment (directly, or through the rendered
    runtime secrets file the services load) and otherwise scrapes public
    engines that block automated clients. A key that exists only in the vault
    is reported separately: the tool does not read the vault. Not repairable
    here: the key is a credential the operator obtains and stores.
    """
    if search_api_configured():
        return ok("Brave Search API key is set in the environment; web_search uses the API first")

    rendered = _in_runtime_file(_runtime_secrets_file())
    if rendered is True:
        return ok(
            "Brave Search API key is in the runtime secrets file the services load; "
            "web_search uses the API first"
        )

    from robothor.secrets import secret_source

    source = secret_source(SEARCH_KEY_ENV)
    if source == "vault":
        return fail(
            f"{SEARCH_KEY_ENV} is in the vault only. web_search reads the engine environment, "
            "not the vault, so it has no API provider: add the key to the secrets store "
            "(sops) and restart robothor-secrets and the engine."
        )
    if rendered is None:
        return fail(
            f"{SEARCH_KEY_ENV} is not in this shell's environment and the runtime secrets "
            "file could not be read to check it — re-run as the service account or with sudo."
        )
    return fail(_MISSING)


CHECKS: tuple[Check, ...] = (
    Check(
        id="search.provider",
        title="Web search has an API provider",
        category="search",
        severity="recommended",
        run=_provider,
    ),
)
