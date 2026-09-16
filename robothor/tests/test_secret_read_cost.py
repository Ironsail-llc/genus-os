"""Reading one credential must not decrypt the whole vault.

Review finding I8. Before the vault went first, an application credential in the
process environment short-circuited before the vault was touched at all. Now
every application read goes to the vault first — and the first cut's
implementation asked ``export_env()``, which opens a psycopg2 connection and
decrypts EVERY row the instance owns, to answer a question about one.

The callers are hot: ``github_api._get_token`` per request,
``slack_credentials``, ``memory/generation._remote_enabled``, ``jira``, and —
worst — ``build_exec_env`` once per grant per ``exec``. On a box doing a few
hundred execs a day that is a connection and a full decrypt each time, and a
much larger window in which every credential the instance owns is in memory at
once.

Two things fix it and both are asserted here: read one ROW by key rather than
exporting everything, and cache what was read for the life of a run, invalidated
by the same hook ``vault_set`` already calls. A rotation must still be visible
immediately — a cache that outlives a write would be the incident again with a
shorter fuse, so that is asserted too.
"""

from __future__ import annotations

import pytest

VALUE = "ghp_FAKE0000aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def counting_vault(monkeypatch):
    """A vault double that counts what it was asked, per call kind."""
    from robothor import secrets as secrets_module
    from robothor import vault

    rows: dict[str, str] = {"providers/github/api_key": VALUE}
    calls: dict[str, list[str]] = {"get": [], "export": []}

    def fake_get(key, **kwargs):
        calls["get"].append(key)
        return rows.get(key)

    def fake_export(**kwargs):
        calls["export"].append("<all rows>")
        return {k.upper().replace("/", "_"): v for k, v in rows.items()}

    monkeypatch.setattr(vault, "get", fake_get)
    monkeypatch.setattr(vault, "export_env", fake_export)
    monkeypatch.setattr(vault, "set", lambda key, value, **kw: rows.__setitem__(key, value))
    secrets_module.reset_vault_availability()
    secrets_module.reset_secret_cache()
    yield rows, calls
    secrets_module.reset_vault_availability()
    secrets_module.reset_secret_cache()


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves, installed BEFORE anything is cached.

    Patching it afterwards would leave entries stamped with the real monotonic
    clock and compared against a fake one — the comparison comes out negative
    and the entry looks fresh forever, so the test would pass against a cache
    with no TTL at all.
    """
    from robothor import secrets as secrets_module

    now = [1000.0]
    monkeypatch.setattr(secrets_module, "_clock", lambda: now[0])

    def advance(seconds: float) -> None:
        now[0] += seconds

    return advance


def test_one_read_does_not_decrypt_every_row(counting_vault, monkeypatch):
    """``export_env`` returns every secret the instance owns. Using it to answer
    a question about ONE is a full decrypt per lookup and a much larger window
    in which all of them are in memory."""
    from robothor.secrets import get_secret

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _rows, calls = counting_vault
    assert get_secret("GITHUB_TOKEN") == VALUE
    assert calls["export"] == [], (
        "resolving one credential decrypted the whole vault; read the row by key"
    )


def test_repeated_reads_cost_one_round_trip_per_key(counting_vault, clock, monkeypatch):
    """``build_exec_env`` resolves once per grant per ``exec``, and the GitHub
    tool resolves per request."""
    from robothor.secrets import get_secret

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _rows, calls = counting_vault
    for _ in range(25):
        assert get_secret("GITHUB_TOKEN") == VALUE
    assert len(calls["get"]) <= 1, (
        f"25 reads of one credential cost {len(calls['get'])} vault round trips"
    )


def test_two_different_keys_each_cost_their_own_round_trip(counting_vault, clock, monkeypatch):
    """A cache that answered for a key it never read would be worse than none."""
    from robothor.secrets import get_secret

    rows, calls = counting_vault
    rows["providers/brave/api_key"] = "brave-FAKE-0000"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    assert get_secret("GITHUB_TOKEN") == VALUE
    assert get_secret("BRAVE_API_KEY") == "brave-FAKE-0000"
    assert get_secret("GITHUB_TOKEN") == VALUE
    assert len([k for k in calls["get"] if "github" in k]) <= 1
    assert len([k for k in calls["get"] if "brave" in k]) <= 1


def test_a_write_makes_the_new_value_visible_at_once(counting_vault, monkeypatch):
    """The cache must not outlive a rotation.

    This is the whole incident with a shorter fuse: a correct write that
    readers cannot see. ``vault_set`` calls ``_reload_cached_readers``, which
    is where the invalidation belongs.
    """
    from robothor import secrets as secrets_module
    from robothor.secrets import get_secret

    rows, _calls = counting_vault
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_secret("GITHUB_TOKEN") == VALUE

    rows["providers/github/api_key"] = "ghp_FAKE9999_the_replacement"
    secrets_module.reset_secret_cache()
    assert get_secret("GITHUB_TOKEN") == "ghp_FAKE9999_the_replacement"


@pytest.mark.asyncio
async def test_vault_set_invalidates_the_cache(counting_vault, monkeypatch):
    """Asserted through the tool, not the helper: the invalidation has to be on
    the path a rotation actually takes."""
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import vault as vault_tools
    from robothor.secrets import get_secret

    monkeypatch.setattr(vault_tools, "credential_tier", lambda agent_id: "operator")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_secret("GITHUB_TOKEN") == VALUE

    await vault_tools.HANDLERS["vault_set"](
        {"key": "providers/github/api_key", "value": "ghp_FAKE7777_rotated"},
        ToolContext(agent_id="main", tenant_id="default"),
    )
    assert get_secret("GITHUB_TOKEN") == "ghp_FAKE7777_rotated", (
        "a reader was served a cached value after the assistant rotated it"
    )


def test_a_miss_is_cached_too(counting_vault, clock, monkeypatch):
    """``build_exec_env`` resolves an UNSET grant on every exec, and
    ``_remote_enabled`` asks about a provider that may not be configured. A
    cache that only remembered hits would leave the common case uncached."""
    from robothor.secrets import get_secret

    _rows, calls = counting_vault
    monkeypatch.delenv("NOT_CONFIGURED_ANYWHERE", raising=False)
    for _ in range(10):
        assert get_secret("NOT_CONFIGURED_ANYWHERE") is None
    assert len(calls["get"]) + len(calls["export"]) <= 2, (
        "an unconfigured credential cost a vault round trip on every lookup"
    )


def test_the_environment_is_still_read_live(counting_vault, monkeypatch):
    """Only the VAULT half is cached. The environment is a dict lookup — free —
    and caching it would make a test that sets a variable not take effect,
    which is a debugging afternoon for whoever hits it."""
    from robothor.secrets import get_secret

    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "fake-first")
    assert get_secret("ROBOTHOR_DB_PASSWORD") == "fake-first"
    monkeypatch.setenv("ROBOTHOR_DB_PASSWORD", "fake-second")
    assert get_secret("ROBOTHOR_DB_PASSWORD") == "fake-second"


# ── R3: the cache must not outlive a write from another process ──────────────


def test_a_cached_value_expires(counting_vault, clock, monkeypatch):
    """Review R3. The cache had no TTL, so `genus vault set`, `genus secrets
    migrate`, `genus channel add` and the setup wizard — all separate processes
    — became invisible to a running engine until an agent's `vault_set`, a
    `POST /api/admin/secrets/reload`, or a restart.

    On `main` and in round 0 a CLI write took effect on the next read. A
    performance fix that costs that is a regression, not a fix.
    """
    from robothor import secrets as secrets_module
    from robothor.secrets import get_secret

    rows, _calls = counting_vault
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_secret("GITHUB_TOKEN") == VALUE

    # Another process rotates the row. Nothing in THIS process is told.
    rows["providers/github/api_key"] = "ghp_FAKE8888_rotated_elsewhere"

    clock(secrets_module.SECRET_CACHE_TTL_SECONDS + 1.0)
    assert get_secret("GITHUB_TOKEN") == "ghp_FAKE8888_rotated_elsewhere", (
        "a write from another process was still invisible after the TTL"
    )


def test_the_ttl_is_short_enough_to_be_a_hiccup_not_an_outage(counting_vault):
    """An operator who adds BRAVE_API_KEY from the CLI and watches web search
    stay dead has no way to tell a cache from a bug."""
    from robothor import secrets as secrets_module

    assert secrets_module.SECRET_CACHE_TTL_SECONDS <= 30.0


def test_a_cached_miss_expires_sooner_than_a_hit(counting_vault, monkeypatch):
    """The asymmetry matters. A cached HIT is a correct answer that is briefly
    stale; a cached MISS is a feature that stays dead — the operator adds the
    key and nothing happens, which reads as broken rather than slow."""
    from robothor import secrets as secrets_module

    assert secrets_module.SECRET_CACHE_MISS_TTL_SECONDS < secrets_module.SECRET_CACHE_TTL_SECONDS


def test_a_name_first_seen_as_missing_is_picked_up(counting_vault, clock, monkeypatch):
    """The probe's second line: `a name cached as MISS, row later written
    externally: missing`. Web search stays dead after the operator adds the
    key from the CLI."""
    from robothor import secrets as secrets_module
    from robothor.secrets import get_secret

    rows, _calls = counting_vault
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert get_secret("BRAVE_API_KEY") is None

    rows["providers/brave/api_key"] = "brave-FAKE-added-from-the-cli"
    clock(secrets_module.SECRET_CACHE_MISS_TTL_SECONDS + 1.0)
    assert get_secret("BRAVE_API_KEY") == "brave-FAKE-added-from-the-cli"


def test_the_reload_endpoint_invalidates_the_cache(counting_vault, monkeypatch):
    """The explicit path, for the callers that can reach the engine: the bridge's
    `POST /api/admin/secrets/reload` must not have to wait out a TTL."""
    from robothor import secrets as secrets_module
    from robothor.engine import key_pool
    from robothor.secrets import get_secret

    rows, _calls = counting_vault
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert get_secret("GITHUB_TOKEN") == VALUE

    rows["providers/github/api_key"] = "ghp_FAKE6666_via_reload"
    monkeypatch.setattr(key_pool, "_provider_env_names", lambda: frozenset())
    key_pool.reload_provider_keys()
    assert get_secret("GITHUB_TOKEN") == "ghp_FAKE6666_via_reload"
    assert secrets_module.SECRET_CACHE_TTL_SECONDS > 0
