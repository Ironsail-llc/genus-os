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
