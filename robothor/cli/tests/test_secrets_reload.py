"""`genus secrets reload` — the command that did not exist on the night it was needed.

2026-09-16: the operator raised the cap at the provider. Nothing changed. The
engine's credential pool holds a key retired for a calendar quota for six
hours, in memory, and neither a top-up nor a raised cap is visible to it. The
only way back without restarting the daemon was

    POST /api/admin/secrets/reload

with a service token in the engine's own audience — the control token the
operator has to hand is rejected with 401, correctly, since it is minted for a
different audience. There was no command for it.

These tests pin the three things that make the command trustworthy: it mints
the right credential, it reports what actually came back, and it never prints
the token.
"""

from __future__ import annotations

import argparse

import pytest

from robothor.cli import secrets_cmd


@pytest.fixture
def signing_key(monkeypatch):
    """A test signing key, so the minted token can be decoded and inspected."""
    from robothor.auth import tokens

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


class TestTheTokenItMints:
    def test_it_is_a_service_token_in_the_engines_audience(self, signing_key):
        from robothor.auth.tokens import decode_token
        from robothor.engine.auth import ENGINE_AUDIENCE
        from robothor.engine_control import control_token

        claims = decode_token(control_token(), expected_audience=ENGINE_AUDIENCE)

        assert claims["aud"] == ENGINE_AUDIENCE
        assert "engine:control" in claims["scope"].split()
        assert claims["typ"] == "service"

    def test_it_refuses_to_mint_when_no_signing_key_resolves(self, monkeypatch):
        """Minting GENERATES a signing key when none is found, and the upsert
        would invalidate every session and every stored MFA secret on a box
        whose vault is merely unreadable. A control call is not worth that."""
        from robothor import engine_control

        monkeypatch.setattr(
            "robothor.secrets.secret_source", lambda *a, **kw: "unavailable", raising=True
        )
        with pytest.raises(engine_control.EngineUnreachableError):
            engine_control.control_token()

    def test_a_bridge_audience_token_would_not_do(self, signing_key):
        """Why the command exists at all: the engine refuses the other audience."""
        from robothor.auth.tokens import TokenError, decode_token, issue_service_token
        from robothor.engine.auth import ENGINE_AUDIENCE

        other = issue_service_token("genus-cli", "t", scopes=("bridge:read",))
        with pytest.raises(TokenError):
            decode_token(other, expected_audience=ENGINE_AUDIENCE)


class _FakeEngine:
    """Stands in for a running daemon's control API."""

    def __init__(self, body):
        self.body = body
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path))
        return self.body


class TestTheCommand:
    def test_it_posts_to_the_reload_route(self, monkeypatch, capsys):
        engine = _FakeEngine({"reloaded": ["openrouter"], "slots": 2, "restored": []})
        monkeypatch.setattr(secrets_cmd, "control_request", engine)

        code = secrets_cmd.cmd_secrets(argparse.Namespace(secrets_command="reload"))

        assert code == 0
        assert engine.calls == [("POST", "/api/admin/secrets/reload")]
        assert "openrouter" in capsys.readouterr().out

    def test_it_names_the_credentials_that_came_back(self, monkeypatch, capsys):
        engine = _FakeEngine(
            {"reloaded": [], "slots": 0, "restored": ["key-1a2b3c4d", "key-5e6f7a8b"]}
        )
        monkeypatch.setattr(secrets_cmd, "control_request", engine)

        secrets_cmd.cmd_secrets(argparse.Namespace(secrets_command="reload"))

        out = capsys.readouterr().out
        assert "key-1a2b3c4d" in out
        assert "back in rotation" in out

    def test_a_reload_that_restored_nothing_says_so(self, monkeypatch, capsys):
        engine = _FakeEngine({"reloaded": [], "slots": 0, "restored": []})
        monkeypatch.setattr(secrets_cmd, "control_request", engine)

        secrets_cmd.cmd_secrets(argparse.Namespace(secrets_command="reload"))

        assert "no credential was out of rotation" in capsys.readouterr().out

    def test_an_engine_that_is_down_is_an_error_not_a_success(self, monkeypatch, capsys):
        from robothor.engine_control import EngineUnreachableError

        def _down(method, path, **kwargs):
            raise EngineUnreachableError("the engine did not answer at http://127.0.0.1:8080")

        monkeypatch.setattr(secrets_cmd, "control_request", _down)

        code = secrets_cmd.cmd_secrets(argparse.Namespace(secrets_command="reload"))

        assert code == 1
        assert "did not answer" in capsys.readouterr().out

    def test_it_never_prints_a_token(self, monkeypatch, capsys, signing_key):
        engine = _FakeEngine({"reloaded": ["openrouter"], "slots": 1, "restored": []})
        monkeypatch.setattr(secrets_cmd, "control_request", engine)

        secrets_cmd.cmd_secrets(argparse.Namespace(secrets_command="reload"))

        out = capsys.readouterr().out
        assert "eyJ" not in out, "a JWT in a terminal is a JWT in a backup"
        assert "Bearer" not in out


class TestTheWriteNotifierAuthenticatesNow:
    """`genus vault set` and friends already told the engine. It was refused.

    `secrets/reload.notify_engine` deliberately sent no credential, so on every
    production instance the POST was answered 401 and the operator was told to
    wait out a cache TTL — while the engine that needed telling sat there
    refusing them. It can mint safely now, so it does.
    """

    def _posted(self, monkeypatch):
        import httpx

        from robothor.secrets import reload as reload_mod

        seen: dict = {}

        def _post(url, timeout=None, headers=None):
            seen["url"] = url
            seen["headers"] = headers or {}
            return httpx.Response(200, json={"reloaded": [], "slots": 0})

        monkeypatch.setattr(httpx, "post", _post)
        reload_mod.notify_engine(quiet=True)
        return seen

    def test_it_carries_a_bearer_when_a_key_resolves(self, monkeypatch, signing_key):
        seen = self._posted(monkeypatch)
        assert seen["headers"].get("Authorization", "").startswith("Bearer ")

    def test_it_still_posts_when_no_credential_can_be_minted(self, monkeypatch):
        from robothor import engine_control

        monkeypatch.setattr(
            engine_control,
            "control_token",
            lambda *a, **kw: (_ for _ in ()).throw(
                engine_control.EngineUnreachableError("no signing key")
            ),
        )
        seen = self._posted(monkeypatch)
        assert "Authorization" not in seen["headers"], "unauthenticated, exactly as before"
        assert seen["url"].endswith("/api/admin/secrets/reload")


class TestTheStatusTableShowsRotation:
    def test_a_retired_key_is_reported_with_its_reason_and_return(self):
        """The columns an operator reads at 02:00, from the LIVE pool."""
        rows = secrets_cmd.rotation_notes(
            {
                "providers": [
                    {
                        "id": "openrouter",
                        "env_var": "OPENROUTER_API_KEY",
                        "slots": [
                            {
                                "fingerprint": "key-1a2b3c4d",
                                "state": "capped",
                                "reason": "quota_exhausted_periodic",
                                "retired_for_s": 7200.0,
                                "returns_in_s": 14400.0,
                            }
                        ],
                    }
                ]
            }
        )

        note = rows["key-1a2b3c4d"]
        assert "retired" in note
        assert "quota_exhausted_periodic" in note
        assert "2.0h ago" in note
        assert "4.0h" in note

    def test_a_key_in_rotation_says_so(self):
        rows = secrets_cmd.rotation_notes(
            {
                "providers": [
                    {
                        "id": "openrouter",
                        "env_var": "OPENROUTER_API_KEY",
                        "slots": [{"fingerprint": "key-9z", "state": "active"}],
                    }
                ]
            }
        )
        assert rows["key-9z"] == "in rotation"

    def test_nothing_is_claimed_when_the_engine_is_not_reachable(self, monkeypatch):
        from robothor.engine_control import EngineUnreachableError

        def _down(method, path, **kwargs):
            raise EngineUnreachableError("down")

        monkeypatch.setattr(secrets_cmd, "control_request", _down)
        assert secrets_cmd.live_rotation() == {}
