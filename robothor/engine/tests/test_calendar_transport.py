from types import SimpleNamespace

import httpx
import pytest

from robothor.engine.calendar_transport import CalendarTransport


@pytest.mark.parametrize("mode", ["valid", "malformed", "timeout", "redirect"])
def test_conditional_patch_fixed_endpoint_and_uncertain_outcome(monkeypatch, mode):
    monkeypatch.setattr(
        "robothor.settings.get_settings",
        lambda: SimpleNamespace(channels=SimpleNamespace(google_workspace_token="fixture-token")),
    )

    def serve(request):
        assert request.url.host == "www.googleapis.com"
        assert request.url.params["sendUpdates"] == "all"
        assert request.headers["If-Match"] == '"v1"'
        if mode == "timeout":
            raise httpx.ReadTimeout("SECRET", request=request)
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://bad.example"})
        return httpx.Response(200, json={"id": "meeting"} if mode == "valid" else {})

    with CalendarTransport() as api:
        api.client.close()
        api.client = httpx.Client(transport=httpx.MockTransport(serve))
        result = api.request(
            "PATCH", "owner@example.com", "meeting", etag='"v1"', body={"attendees": []}
        )
    if mode == "valid":
        assert result == {"id": "meeting"}
    else:
        assert "error" in result
        assert "SECRET" not in str(result)
        if mode in {"malformed", "timeout"}:
            assert result["outcome_unknown"]


def test_auth_failure_does_not_expose_credentials(monkeypatch):
    def fail(self):
        raise ValueError("private refresh token")

    monkeypatch.setattr(CalendarTransport, "_authenticate", fail)
    with CalendarTransport() as api:
        result = api.request("GET", "owner@example.com", "meeting")
    assert result == {"error": "Calendar authentication failed", "status_code": 401}


def test_plain_credentials_follow_gws_config_directory(monkeypatch, tmp_path):
    import json
    import subprocess

    credentials = {
        "client_id": "fixture-client",
        "client_secret": "fixture-secret",
        "refresh_token": "fixture-refresh",
    }
    (tmp_path / "credentials.json").write_text(json.dumps(credentials))
    monkeypatch.setattr(
        "robothor.settings.get_settings",
        lambda: SimpleNamespace(
            channels=SimpleNamespace(
                google_workspace_token="",
                google_workspace_credentials_file="",
                google_workspace_config_dir=str(tmp_path),
            )
        ),
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: pytest.fail("Plain credentials must not use export")
    )

    def serve(request):
        assert request.url.host == "oauth2.googleapis.com"
        assert b"fixture-refresh" in request.content
        return httpx.Response(200, json={"access_token": "fixture-access"})

    with CalendarTransport() as api:
        api.client.close()
        api.client = httpx.Client(transport=httpx.MockTransport(serve))
        api._authenticate()
        assert api.token == "fixture-access"
