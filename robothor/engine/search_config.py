"""The one place the web-search API key is read from the environment.

``web_search`` (the tool), the search-degradation detector and the doctor
check all need to know whether the instance has an API provider. Reading the
variable in each of them would be three environment-read sites for one fact
— and the settings ratchet (``tests/test_settings_registry.py``) exists so
that number only goes down.
"""

from __future__ import annotations

import os

SEARCH_KEY_ENV = "BRAVE_SEARCH_API_KEY"


def brave_search_key() -> str:
    """The Brave Search API key, or an empty string when the instance has none."""
    return os.environ.get(SEARCH_KEY_ENV, "").strip()


def search_api_configured() -> bool:
    """True when ``web_search`` has an API provider to prefer over scraping."""
    return bool(brave_search_key())
