"""The first-run wizard's API: public by necessity, narrow by construction.

These routes create the owner account, store provider credentials and install
agents, and they answer a caller with no session at all — because on a fresh
box there is nobody to have one. Everything here is about the three walls that
make that safe:

1. **They exist only before there is an owner.** ``setup_complete()`` asks the
   DATABASE, and the moment it says yes every route in this router answers 404
   — status included. A completed appliance has no first-run surface to find.
2. **A setup token, then a claim token.** The token was printed once by
   ``genus init``; it buys a five-minute claim, and the claim is what every
   other route requires. A session token is not a claim and a claim is not a
   session.
3. **Nothing that goes in comes back out.** A provider key, a bot token and a
   password enter through here and appear in no response body, no audit row
   and no log line — asserted by walking every response for the fixture
   values.

The parametrised 404 test at the bottom is the one that must never be deleted:
it enumerates the router's own routes off the app, so a route added later
without the gate fails it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# Fixture credentials. Generic on purpose (CLAUDE.md rule 1) and asserted
# ABSENT from every response: if one of these strings ever appears in a body,
# a real credential would have too.
FIXTURE_TOKEN_UNUSED = "not-the-printed-token"
FIXTURE_API_KEY = "sk-test-1111111111111111111111111"
FIXTURE_BOT_TOKEN = "1234567890:test-bot-token-value-aaaaaaaaaa"
FIXTURE_PASSWORD = "correct-horse-battery-staple"
OPERATOR_EMAIL = "alice@example.test"

SETUP_ROUTES = [
    ("GET", "/api/setup/status"),
    ("POST", "/api/setup/claim"),
    ("GET", "/api/setup/detect"),
    ("POST", "/api/setup/operator"),
    ("POST", "/api/setup/provider"),
    ("POST", "/api/setup/channel"),
    ("POST", "/api/setup/agent"),
    ("POST", "/api/setup/complete"),
]


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A throwaway workspace. The router writes a token file and a config.yaml
    into it, and must never be pointed at the operator's real one."""
    directory = tmp_path / "workspace"
    (directory / ".robothor").mkdir(parents=True)
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(directory))
    import routers.setup as setup_router

    monkeypatch.setattr(setup_router, "workspace", lambda: directory)
    return directory


@pytest.fixture
def incomplete(monkeypatch):
    """No owner account: the whole router is mounted and answering."""
    monkeypatch.setattr("robothor.auth.accounts.owner_account_exists", lambda *a, **k: False)


@pytest.fixture
def signing_key(monkeypatch):
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.fixture
def printed_token(workspace) -> str:
    from robothor import setup_token

    return setup_token.create_setup_token(workspace)


@pytest.fixture(autouse=True)
def _fresh_limiter():
    from robothor.auth import local_login

    local_login.reset_rate_limiter()
    yield
    local_login.reset_rate_limiter()


@pytest.fixture
async def claim(test_client, incomplete, signing_key, printed_token) -> str:
    response = await test_client.post("/api/setup/claim", json={"token": printed_token})
    assert response.status_code == 200, response.text
    return str(response.json()["claim_token"])


def _auth(claim_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {claim_token}"}


def _assert_no_secret(*payloads: object) -> None:
    """No fixture credential may appear in anything we hand back."""
    blob = json.dumps(list(payloads), default=str)
    for secret in (FIXTURE_API_KEY, FIXTURE_BOT_TOKEN, FIXTURE_PASSWORD):
        assert secret not in blob


# ── status ───────────────────────────────────────────────────────────


class TestStatus:
    async def test_is_public(self, test_client, incomplete, workspace):
        response = await test_client.get("/api/setup/status")

        assert response.status_code == 200
        assert response.json()["complete"] is False

    async def test_carries_only_booleans(self, test_client, incomplete, workspace):
        """A public route on an unclaimed box says what is DONE, never what is
        configured: an email, a provider id or a hostname here would be a map
        for whoever finds the port first."""
        payload = await test_client.get("/api/setup/status")

        body = payload.json()
        assert set(body) == {"complete", "steps"}
        assert all(isinstance(value, bool) for value in body["steps"].values())

    async def test_needs_no_claim_token(self, test_client, incomplete, workspace):
        """proxy.ts polls this before anyone has anything."""
        assert (await test_client.get("/api/setup/status")).status_code == 200


# ── claim ────────────────────────────────────────────────────────────


class TestClaim:
    async def test_exchanges_the_printed_token_for_a_claim(
        self, test_client, incomplete, signing_key, printed_token
    ):
        response = await test_client.post("/api/setup/claim", json={"token": printed_token})

        assert response.status_code == 200
        body = response.json()
        assert body["expires_in"] == 300
        assert printed_token not in json.dumps(body)

    async def test_the_claim_is_a_setup_token_not_a_session(
        self, test_client, incomplete, signing_key, printed_token
    ):
        from robothor.auth import tokens

        response = await test_client.post("/api/setup/claim", json={"token": printed_token})

        claims = tokens.decode_setup_claim_token(response.json()["claim_token"])
        assert claims["typ"] == "setup"
        with pytest.raises(tokens.TokenError):
            tokens.decode_token(response.json()["claim_token"])

    async def test_a_wrong_token_is_one_generic_401(
        self, test_client, incomplete, signing_key, printed_token
    ):
        response = await test_client.post("/api/setup/claim", json={"token": FIXTURE_TOKEN_UNUSED})

        assert response.status_code == 401
        assert response.json() == {"error": "invalid or expired token"}

    async def test_no_token_file_gives_the_same_401(self, test_client, incomplete, workspace):
        """ "Nobody ran init here" and "wrong token" must be indistinguishable."""
        response = await test_client.post("/api/setup/claim", json={"token": FIXTURE_TOKEN_UNUSED})

        assert response.status_code == 401
        assert response.json() == {"error": "invalid or expired token"}

    async def test_a_consumed_token_gives_the_same_401(
        self, test_client, incomplete, signing_key, printed_token, workspace
    ):
        from robothor import setup_token

        setup_token.consume_setup_token(workspace)

        response = await test_client.post("/api/setup/claim", json={"token": printed_token})

        assert response.status_code == 401
        assert response.json() == {"error": "invalid or expired token"}

    async def test_is_rate_limited_at_five_a_minute(
        self, test_client, incomplete, signing_key, printed_token
    ):
        codes = []
        for _ in range(7):
            response = await test_client.post(
                "/api/setup/claim", json={"token": FIXTURE_TOKEN_UNUSED}
            )
            codes.append(response.status_code)

        assert codes[:5] == [401] * 5
        assert codes[5:] == [429, 429]

    async def test_a_correct_token_does_not_escape_the_limit(
        self, test_client, incomplete, signing_key, printed_token
    ):
        """Otherwise the limiter is bypassable by anyone who guesses once."""
        for _ in range(5):
            await test_client.post("/api/setup/claim", json={"token": FIXTURE_TOKEN_UNUSED})

        response = await test_client.post("/api/setup/claim", json={"token": printed_token})

        assert response.status_code == 429

    async def test_a_malformed_body_is_refused_without_echo(
        self, test_client, incomplete, signing_key
    ):
        response = await test_client.post("/api/setup/claim", json={"nope": FIXTURE_API_KEY})

        assert response.status_code in (400, 401)
        _assert_no_secret(response.json())

    async def test_the_claim_route_does_not_consume_the_setup_token(
        self, test_client, incomplete, signing_key, printed_token, workspace
    ):
        """The token is spent by the OPERATOR step, not by claiming. A claim
        that burned it would leave a browser that reloaded the page unable to
        try again, with no way to recover but a new `genus auth setup-link`."""
        from robothor import setup_token

        await test_client.post("/api/setup/claim", json={"token": printed_token})

        assert setup_token.verify_setup_token(workspace, printed_token) is True


# ── the claim gate on every other route ──────────────────────────────


class TestClaimGate:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            route
            for route in SETUP_ROUTES
            if route[1] not in {"/api/setup/status", "/api/setup/claim"}
        ],
    )
    async def test_refused_without_a_claim(self, test_client, incomplete, workspace, method, path):
        response = await test_client.request(method, path, json={})

        assert response.status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            route
            for route in SETUP_ROUTES
            if route[1] not in {"/api/setup/status", "/api/setup/claim"}
        ],
    )
    async def test_an_operator_session_is_not_a_claim(
        self, test_client, incomplete, signing_key, workspace, method, path
    ):
        """Requirement: /api/setup/* is never reachable with a session. A stolen
        cookie must not reopen the routes that create an owner."""
        from robothor.auth import tokens

        session = tokens.issue_access_token("user-1", "default", "owner")

        response = await test_client.request(
            method, path, json={}, headers={"Authorization": f"Bearer {session}"}
        )

        assert response.status_code == 401

    async def test_a_garbage_bearer_is_refused(
        self, test_client, incomplete, signing_key, workspace
    ):
        response = await test_client.get(
            "/api/setup/detect", headers={"Authorization": "Bearer not-a-jwt"}
        )

        assert response.status_code == 401


# ── detect ───────────────────────────────────────────────────────────


class TestDetect:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch):
        import routers.setup as setup_router

        monkeypatch.setattr(
            setup_router,
            "_provider_state",
            lambda: [
                {
                    "id": "openrouter",
                    "configured": True,
                    "slots": [{"position": 1, "source": "vault", "fingerprint": "sha256:ab12"}],
                }
            ],
        )
        monkeypatch.setattr(
            setup_router,
            "_ollama_state",
            lambda: {"reachable": True, "tool_models": ["qwen3:8b"]},
        )
        monkeypatch.setattr(setup_router, "_telegram_configured", lambda: False)
        monkeypatch.setattr(
            setup_router,
            "_required_check_rows",
            lambda: [{"id": "db.connect", "status": "pass"}],
        )

        async def catalog():
            return [{"id": "openrouter/openai/gpt-5.4", "provider": "openrouter"}]

        monkeypatch.setattr(setup_router, "_model_catalog", catalog)

    async def test_offers_the_model_catalogue(self, test_client, claim, workspace):
        """`GET /api/models` is operator-gated and this caller has no session,
        so the ids the wizard's select needs come through here. Nothing in the
        list is a secret."""
        response = await test_client.get("/api/setup/detect", headers=_auth(claim))

        assert response.json()["models"] == [
            {"id": "openrouter/openai/gpt-5.4", "provider": "openrouter"}
        ]

    async def test_reports_what_the_box_already_has(self, test_client, claim, workspace):
        response = await test_client.get("/api/setup/detect", headers=_auth(claim))

        assert response.status_code == 200
        body = response.json()
        assert body["providers"][0]["id"] == "openrouter"
        assert body["ollama"]["tool_models"] == ["qwen3:8b"]
        assert body["telegram"]["configured"] is False
        assert body["doctor"]["checks"] == [{"id": "db.connect", "status": "pass"}]

    async def test_carries_fingerprints_and_never_keys(self, test_client, claim, workspace):
        response = await test_client.get("/api/setup/detect", headers=_auth(claim))

        slot = response.json()["providers"][0]["slots"][0]
        assert slot["fingerprint"].startswith("sha256:")
        assert "api_key" not in slot
        _assert_no_secret(response.json())

    async def test_doctor_rows_are_ids_and_statuses_only(self, test_client, claim, workspace):
        """The full report enumerates what is wrong with the appliance, which is
        a map for anyone who should not have it. The strip needs two fields."""
        response = await test_client.get("/api/setup/detect", headers=_auth(claim))

        for row in response.json()["doctor"]["checks"]:
            assert set(row) == {"id", "status"}


# ── operator ─────────────────────────────────────────────────────────


class FakeAccounts:
    """The account DAL, with one in-memory owner row."""

    def __init__(self) -> None:
        self.owner: dict | None = None
        self.passwords: dict[str, str] = {}

    def owner_account_exists(self, tenant_id=None) -> bool:
        return self.owner is not None

    def bootstrap_owner_account(self) -> dict | None:
        from robothor.owner_config import load_owner_config

        config = load_owner_config()
        if config is None or not config.email:
            return None
        self.owner = {
            "id": "00000000-0000-0000-0000-0000000000aa",
            "tenant_id": config.tenant_id,
            "email": config.email,
            "display_name": f"{config.first_name} {config.last_name}".strip(),
            "role": "owner",
            "status": "active",
            "mfa_enabled": False,
        }
        return dict(self.owner)

    def get_account_by_email(self, tenant_id: str, email: str) -> dict | None:
        if self.owner and self.owner["email"] == email.lower():
            return dict(self.owner)
        return None


@pytest.fixture
def fake_accounts(monkeypatch):
    accounts = FakeAccounts()
    monkeypatch.setattr(
        "robothor.auth.accounts.owner_account_exists", accounts.owner_account_exists
    )
    monkeypatch.setattr(
        "robothor.auth.accounts.bootstrap_owner_account", accounts.bootstrap_owner_account
    )
    monkeypatch.setattr(
        "robothor.auth.accounts.get_account_by_email", accounts.get_account_by_email
    )
    return accounts


@pytest.fixture
def fake_login(monkeypatch):
    """`set_password` and `authenticate` without a database."""
    recorded: dict = {"password_set_for": None}

    def set_password(user_id: str, password: str) -> None:
        recorded["password_set_for"] = user_id
        recorded["password"] = password

    def authenticate(tenant_id, email, password, code=None, **kwargs):
        from robothor.auth.local_login import LoginResult

        return LoginResult(
            ok=True,
            tokens={
                "access_token": "access-token-value",
                "refresh_token": "refresh-token-value",
                "user": {
                    "id": "00000000-0000-0000-0000-0000000000aa",
                    "email": email,
                    "display_name": "Alice",
                    "role": "owner",
                    "tenant_id": tenant_id,
                },
            },
            mfa_setup_required=True,
            error="",
        )

    monkeypatch.setattr("robothor.auth.local_login.set_password", set_password)
    monkeypatch.setattr("robothor.auth.local_login.authenticate", authenticate)
    return recorded


@pytest.fixture
def owner_home(tmp_path, monkeypatch) -> Path:
    """Where ``owner.yaml`` lands. Never the real ``~/.robothor``."""
    path = tmp_path / "owner-home" / "owner.yaml"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(path))
    from robothor.settings import reset_settings

    reset_settings()
    yield path
    reset_settings()


def _operator_body(**overrides) -> dict:
    body = {
        "name": "Alice Example",
        "email": OPERATOR_EMAIL,
        "password": FIXTURE_PASSWORD,
    }
    body.update(overrides)
    return body


class TestOperator:
    async def test_creates_the_owner_and_signs_the_browser_in(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_token"] == "access-token-value"
        assert body["user"]["role"] == "owner"
        assert body["mfa_setup_required"] is True
        assert fake_accounts.owner is not None

    async def test_writes_owner_yaml_under_the_same_tenant_as_the_account(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        """The live doctor found an instance whose owner.yaml named one tenant
        while the account lived under another. That class must be impossible
        here: the account is derived FROM the file."""
        from robothor.owner_config import load_owner_config

        await test_client.post(
            "/api/setup/operator",
            json=_operator_body(tenant_id="acme"),
            headers=_auth(claim),
        )

        config = load_owner_config(owner_home)
        assert config is not None
        assert config.tenant_id == "acme"
        assert config.email == OPERATOR_EMAIL
        assert fake_accounts.owner["tenant_id"] == "acme"

    async def test_turns_local_login_on_in_config_yaml(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        """Without this the account exists and there is no way to use it."""
        import yaml

        await test_client.post("/api/setup/operator", json=_operator_body(), headers=_auth(claim))

        document = yaml.safe_load((workspace / ".robothor" / "config.yaml").read_text())
        assert document["settings"]["auth"]["local_login"] is True

    async def test_consumes_the_setup_token(
        self, test_client, claim, workspace, printed_token, fake_accounts, fake_login, owner_home
    ):
        from robothor import setup_token

        await test_client.post("/api/setup/operator", json=_operator_body(), headers=_auth(claim))

        assert setup_token.verify_setup_token(workspace, printed_token) is False

    async def test_refuses_when_an_owner_already_exists(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home, monkeypatch
    ):
        """The router's OWN lock, tested with the 404 gate held open so that it
        cannot be the gate answering. Two locks, because the gate is computed
        one moment before the insert."""
        import routers.setup as setup_router

        fake_accounts.owner = {"id": "x", "tenant_id": "default", "email": "bob@example.test"}
        monkeypatch.setattr(setup_router, "setup_complete", lambda *a, **k: False)

        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )

        assert response.status_code == 409

    async def test_a_second_attempt_is_409(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home, monkeypatch
    ):
        import routers.setup as setup_router

        monkeypatch.setattr(setup_router, "setup_complete", lambda *a, **k: False)
        first = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )
        assert first.status_code == 200

        second = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )

        assert second.status_code == 409

    async def test_refuses_an_existing_owner_yaml_naming_someone_else(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        """A half-finished earlier run must not silently create the account
        under an identity the operator did not just type."""
        owner_home.write_text(
            "tenant_id: default\nfirst_name: Bob\nlast_name: Other\nemail: bob@example.test\n"
        )

        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )

        assert response.status_code == 409
        assert fake_accounts.owner is None

    async def test_a_stale_owner_env_var_cannot_hijack_the_identity(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home, monkeypatch
    ):
        """`ROBOTHOR_OWNER_EMAIL` is deprecated but still read as a FALLBACK when
        no owner.yaml exists. A stale one in the service environment must not
        refuse the operator at the wizard, and must not end up owning the
        appliance — the file this step writes is authoritative over it."""
        from robothor.owner_config import load_owner_config
        from robothor.settings import reset_settings

        monkeypatch.setenv("ROBOTHOR_OWNER_EMAIL", "someone-else@example.test")
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "Someone Else")
        reset_settings()

        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )

        assert response.status_code == 200, response.text
        assert load_owner_config(owner_home).email == OPERATOR_EMAIL
        assert fake_accounts.owner["email"] == OPERATOR_EMAIL

    async def test_refuses_a_weak_password_without_echoing_it(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(password="short"), headers=_auth(claim)
        )

        assert response.status_code == 422
        assert "short" not in response.text
        assert fake_accounts.owner is None

    async def test_never_echoes_the_password(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
        )

        _assert_no_secret(response.json())

    async def test_refuses_a_malformed_email(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        response = await test_client.post(
            "/api/setup/operator", json=_operator_body(email="not-an-email"), headers=_auth(claim)
        )

        assert response.status_code == 422
        assert fake_accounts.owner is None

    async def test_writes_an_audit_row(
        self, test_client, claim, workspace, fake_accounts, fake_login, owner_home
    ):
        with patch("routers._audit.log_event") as log_event:
            await test_client.post(
                "/api/setup/operator", json=_operator_body(), headers=_auth(claim)
            )

        events = [call for call in log_event.call_args_list if call.args[0].startswith("setup.")]
        assert events, "the operator step wrote no audit row"
        assert events[0].kwargs["actor"] == "setup"
        assert FIXTURE_PASSWORD not in json.dumps(events[0].kwargs, default=str)


# ── provider ─────────────────────────────────────────────────────────


class TestProvider:
    @pytest.fixture
    def fake_provider_path(self, monkeypatch):
        calls: dict = {"key": None, "defaults": None, "test": None}

        async def set_key(provider_id, api_key):
            calls["key"] = (provider_id, api_key)

        async def set_defaults(model):
            calls["defaults"] = model

        async def test_connection(provider_id, model):
            calls["test"] = (provider_id, model)
            return 200, {
                "ok": True,
                "model": model,
                "latency_ms": 42,
                "error_class": None,
            }

        import routers.setup as setup_router

        monkeypatch.setattr(setup_router, "_store_provider_key", set_key)
        monkeypatch.setattr(setup_router, "_store_default_model", set_defaults)
        monkeypatch.setattr(setup_router, "_test_provider", test_connection)
        return calls

    async def test_stores_the_key_and_reports_a_working_connection(
        self, test_client, claim, workspace, fake_provider_path
    ):
        response = await test_client.post(
            "/api/setup/provider",
            json={
                "provider_id": "openrouter",
                "api_key": FIXTURE_API_KEY,
                "default_model": "openrouter/openai/gpt-5.4",
            },
            headers=_auth(claim),
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert fake_provider_path["key"] == ("openrouter", FIXTURE_API_KEY)
        assert fake_provider_path["defaults"] == "openrouter/openai/gpt-5.4"

    async def test_never_echoes_the_key(self, test_client, claim, workspace, fake_provider_path):
        response = await test_client.post(
            "/api/setup/provider",
            json={
                "provider_id": "openrouter",
                "api_key": FIXTURE_API_KEY,
                "default_model": "openrouter/openai/gpt-5.4",
            },
            headers=_auth(claim),
        )

        _assert_no_secret(response.json())

    async def test_a_failed_connection_is_reported_not_hidden(
        self, test_client, claim, workspace, monkeypatch
    ):
        """`ok: false` is what blocks the step in the UI, so it must reach it
        with a 200 rather than an error the client renders as "try again"."""
        import routers.setup as setup_router

        async def failing(provider_id, model):
            return 200, {
                "ok": False,
                "model": model,
                "latency_ms": 0,
                "error_class": "AuthenticationError",
            }

        monkeypatch.setattr(setup_router, "_store_provider_key", lambda *a: _noop())
        monkeypatch.setattr(setup_router, "_store_default_model", lambda *a: _noop())
        monkeypatch.setattr(setup_router, "_test_provider", failing)

        response = await test_client.post(
            "/api/setup/provider",
            json={
                "provider_id": "openrouter",
                "api_key": FIXTURE_API_KEY,
                "default_model": "openrouter/openai/gpt-5.4",
            },
            headers=_auth(claim),
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "model": "openrouter/openai/gpt-5.4",
            "latency_ms": 0,
            "error_class": "AuthenticationError",
        }

    async def test_refuses_an_empty_key(self, test_client, claim, workspace, fake_provider_path):
        response = await test_client.post(
            "/api/setup/provider",
            json={"provider_id": "openrouter", "api_key": "", "default_model": "m"},
            headers=_auth(claim),
        )

        assert response.status_code == 422
        assert fake_provider_path["key"] is None

    async def test_the_audit_row_carries_a_fingerprint_not_a_key(
        self, test_client, claim, workspace, fake_provider_path
    ):
        with patch("routers._audit.log_event") as log_event:
            await test_client.post(
                "/api/setup/provider",
                json={
                    "provider_id": "openrouter",
                    "api_key": FIXTURE_API_KEY,
                    "default_model": "openrouter/openai/gpt-5.4",
                },
                headers=_auth(claim),
            )

        blob = json.dumps([c.kwargs for c in log_event.call_args_list], default=str)
        assert FIXTURE_API_KEY not in blob
        assert "sha256:" in blob


async def _noop() -> None:
    return None


# ── channel ──────────────────────────────────────────────────────────


class TestChannel:
    @pytest.fixture
    def fake_telegram(self, monkeypatch):
        stored: dict = {}

        async def verify(token: str):
            return (True, "genus_test_bot") if token == FIXTURE_BOT_TOKEN else (False, "")

        async def store(token: str):
            stored["token"] = token

        import routers.setup as setup_router

        monkeypatch.setattr(setup_router, "_verify_telegram_token", verify)
        monkeypatch.setattr(setup_router, "_store_telegram_token", store)
        return stored

    async def test_verifies_then_stores(self, test_client, claim, workspace, fake_telegram):
        response = await test_client.post(
            "/api/setup/channel",
            json={"telegram_bot_token": FIXTURE_BOT_TOKEN},
            headers=_auth(claim),
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["bot"] == "genus_test_bot"
        assert fake_telegram["token"] == FIXTURE_BOT_TOKEN

    async def test_a_rejected_token_is_not_stored(
        self, test_client, claim, workspace, fake_telegram
    ):
        response = await test_client.post(
            "/api/setup/channel",
            json={"telegram_bot_token": "0000000:not-a-real-token"},
            headers=_auth(claim),
        )

        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert "token" not in fake_telegram

    async def test_never_echoes_the_bot_token(self, test_client, claim, workspace, fake_telegram):
        response = await test_client.post(
            "/api/setup/channel",
            json={"telegram_bot_token": FIXTURE_BOT_TOKEN},
            headers=_auth(claim),
        )

        _assert_no_secret(response.json())

    async def test_skipping_is_a_supported_answer(
        self, test_client, claim, workspace, fake_telegram
    ):
        """The wizard has a Skip button; a channel-free instance is normal."""
        response = await test_client.post(
            "/api/setup/channel", json={"telegram_bot_token": ""}, headers=_auth(claim)
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True, "bot": "", "skipped": True}
        assert "token" not in fake_telegram


# ── agents ───────────────────────────────────────────────────────────


class TestAgent:
    @pytest.fixture
    def fake_installer(self, monkeypatch):
        import routers.setup as setup_router

        def install_preset(preset, **kwargs):
            if preset == "nonsense":
                return {
                    "unknown_preset": True,
                    "available": ["minimal", "standard", "full"],
                    "requested": 0,
                    "installed": [],
                    "failed": {},
                    "missing": [],
                }
            return {
                "unknown_preset": False,
                "available": ["minimal", "standard", "full"],
                "requested": 2,
                "installed": ["main", "concierge"],
                "failed": {},
                "missing": [],
            }

        monkeypatch.setattr(setup_router, "install_preset", install_preset)

    async def test_installs_a_preset_and_names_the_agents(
        self, test_client, claim, workspace, fake_installer
    ):
        response = await test_client.post(
            "/api/setup/agent", json={"preset": "standard"}, headers=_auth(claim)
        )

        assert response.status_code == 200
        assert response.json()["installed"] == ["main", "concierge"]

    async def test_an_unknown_preset_is_422(self, test_client, claim, workspace, fake_installer):
        response = await test_client.post(
            "/api/setup/agent", json={"preset": "nonsense"}, headers=_auth(claim)
        )

        assert response.status_code == 422

    async def test_runs_in_process_not_as_a_subprocess(
        self, test_client, claim, workspace, fake_installer
    ):
        """A shelled-out CLI would run under a different interpreter and
        workspace and report its outcome through parsed stdout."""
        import routers.setup as setup_router

        source = Path(setup_router.__file__).read_text(encoding="utf-8")
        assert "subprocess" not in source


# ── complete ─────────────────────────────────────────────────────────


class TestComplete:
    @pytest.fixture(autouse=True)
    def _doctor(self, monkeypatch):
        import routers.setup as setup_router

        monkeypatch.setattr(
            setup_router, "_required_check_rows", lambda: [{"id": "db.connect", "status": "pass"}]
        )

    async def test_requires_an_owner_account(self, test_client, claim, workspace):
        """Nothing else in the wizard is load-bearing if this step can be called
        on a box with no account: it is what flips the instance closed."""
        response = await test_client.post("/api/setup/complete", headers=_auth(claim))

        assert response.status_code == 409

    async def test_records_completion_and_points_at_the_chat(
        self, test_client, claim, workspace, fake_accounts, monkeypatch
    ):
        import routers.setup as setup_router
        import yaml

        fake_accounts.owner = {"id": "x", "tenant_id": "default", "email": OPERATOR_EMAIL}
        monkeypatch.setattr(setup_router, "setup_complete", lambda *a, **k: False)

        response = await test_client.post("/api/setup/complete", headers=_auth(claim))

        assert response.status_code == 200
        assert response.json()["next"] == "/?v=chat"
        document = yaml.safe_load((workspace / ".robothor" / "config.yaml").read_text())
        assert document["setup_completed_at"].endswith("Z")

    async def test_the_marker_is_not_a_settings_key(
        self, test_client, claim, workspace, fake_accounts, monkeypatch
    ):
        """`settings:` is validated against the registry, and an undeclared key
        in it is rejected under strict mode — which would make a completed
        instance refuse to start."""
        import routers.setup as setup_router
        import yaml

        fake_accounts.owner = {"id": "x", "tenant_id": "default", "email": OPERATOR_EMAIL}
        monkeypatch.setattr(setup_router, "setup_complete", lambda *a, **k: False)

        await test_client.post("/api/setup/complete", headers=_auth(claim))

        document = yaml.safe_load((workspace / ".robothor" / "config.yaml").read_text())
        assert "setup_completed_at" not in (document.get("settings") or {})


# ── the gate: every route disappears once an owner exists ────────────


class TestTheRouterDisappears:
    @pytest.fixture
    def complete(self, monkeypatch):
        monkeypatch.setattr("robothor.auth.accounts.owner_account_exists", lambda *a, **k: True)

    @pytest.mark.parametrize(("method", "path"), SETUP_ROUTES)
    async def test_every_route_404s_once_setup_is_complete(
        self, test_client, complete, signing_key, workspace, method, path
    ):
        response = await test_client.request(method, path, json={})

        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"

    @pytest.mark.parametrize(("method", "path"), SETUP_ROUTES)
    async def test_a_live_claim_does_not_survive_completion(
        self,
        test_client,
        incomplete,
        signing_key,
        workspace,
        printed_token,
        method,
        path,
        monkeypatch,
    ):
        """The claim outlives the token by five minutes, so the gate has to be
        checked on every request rather than once at claim time."""
        response = await test_client.post("/api/setup/claim", json={"token": printed_token})
        claim_token = response.json()["claim_token"]

        monkeypatch.setattr("robothor.auth.accounts.owner_account_exists", lambda *a, **k: True)
        after = await test_client.request(method, path, json={}, headers=_auth(claim_token))

        assert after.status_code == 404

    async def test_the_gate_is_the_database_not_the_token_file(
        self, test_client, complete, signing_key, workspace, printed_token
    ):
        """Deleting the token file must not reopen anything, and neither must
        writing a fresh one."""
        response = await test_client.get("/api/setup/status")

        assert response.status_code == 404

    async def test_enumerates_every_route_the_router_actually_has(self):
        """The parametrised tests above are only as good as their list. This
        fails when a route is added to the router without being covered."""
        import routers.setup as setup_router

        declared = {
            (method, route.path)
            for route in setup_router.router.routes
            for method in getattr(route, "methods", set())
            if method not in {"HEAD", "OPTIONS"}
        }

        assert declared == set(SETUP_ROUTES)


# ── the BFF must never proxy these ───────────────────────────────────


class TestNotProxiedWithASession:
    def test_the_dashboard_bff_denies_the_setup_prefix(self):
        """A session-bearing proxy in front of these routes would undo the whole
        design: the BFF attaches the browser's bridge token to everything it
        forwards.

        Asserted on the exported PATTERN, not on the file's prose — a test that
        greps the whole source passes on a comment. The behaviour itself is
        covered in ``app/__tests__/lib/bridge-proxy-policy.test.ts``; this is
        the bridge side refusing to ship without the dashboard side.
        """
        policy = Path(__file__).resolve().parents[3] / "app" / "src" / "lib"
        source = (policy / "bridge-proxy-policy.ts").read_text(encoding="utf-8")

        declaration = source.split("DENIED_BRIDGE_PATHS", 1)[1]
        pattern = declaration.split(";", 1)[0]
        assert "setup" in pattern, f"the BFF denylist does not cover /api/setup: {pattern}"


# ── middleware ───────────────────────────────────────────────────────


class TestMiddleware:
    def test_only_the_setup_router_lives_under_the_setup_prefix(self):
        """The auth middleware lets `/api/setup/*` past without a session, so
        nothing else may ever be mounted there.

        Routes are resolved through ``iter_route_contexts``: FastAPI >= 0.139
        keeps an included router as one opaque entry in ``app.routes``, so a
        naive loop sees the docs routes and nothing else — a guard that passes
        because it inspected nothing."""
        import routers.setup as setup_router
        from bridge_service import app

        try:
            from fastapi.routing import iter_route_contexts

            resolved = list(iter_route_contexts(app.routes))
        except ImportError:  # pragma: no cover - FastAPI < 0.139
            resolved = list(app.routes)

        ours = {route.path for route in setup_router.router.routes}
        under_prefix = {
            path
            for route in resolved
            if (path := getattr(route, "path", "") or "").startswith("/api/setup")
        }

        assert under_prefix == ours
