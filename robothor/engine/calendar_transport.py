"""Conditional Calendar requests using the deployment's existing gws identity.

gws currently exposes no request-header option. Attendee merges require
If-Match, so use its credential source with the fixed Google endpoints. No
credentials or raw auth failures are returned to the agent or logs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


class CalendarTransport:
    def __init__(self) -> None:
        self.client = httpx.Client(timeout=20.0, follow_redirects=False)
        self.token = ""

    def __enter__(self) -> CalendarTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.client.close()

    def _authenticate(self) -> None:
        from robothor.engine.tools.handlers.gws import _resolve_gws_binary
        from robothor.settings import get_settings

        settings = get_settings().channels
        token = settings.google_workspace_token
        if token:
            self.token = token
            return
        credential_file = settings.google_workspace_credentials_file
        if credential_file:
            credentials = json.loads(Path(credential_file).read_text())
        else:
            proc = subprocess.run(
                [_resolve_gws_binary(), "auth", "export"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            credentials = json.loads(proc.stdout)
        # Only authorized-user credentials are supported here. Never follow
        # a token_uri supplied by a file or export.
        response = self.client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": credentials["refresh_token"],
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
            },
        )
        response.raise_for_status()
        self.token = response.json()["access_token"]

    def request(
        self,
        method: str,
        calendar_id: str,
        event_id: str,
        *,
        body: dict[str, Any] | None = None,
        etag: str = "",
    ) -> dict[str, Any]:
        if not self.token:
            try:
                self._authenticate()
            except Exception:
                return {"error": "Calendar authentication failed", "status_code": 401}
        headers = {"Authorization": f"Bearer {self.token}"}
        if etag:
            headers["If-Match"] = etag
        url = (
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
        )
        try:
            response = self.client.request(
                method,
                url,
                headers=headers,
                json=body,
                params={"sendUpdates": "all"} if method == "PATCH" else None,
            )
            if not response.is_success:
                return {
                    "error": f"Calendar HTTP {response.status_code}",
                    "status_code": response.status_code,
                }
            result = response.json()
            if not isinstance(result, dict) or result.get("id") != event_id:
                return {
                    "error": "Calendar returned an invalid event",
                    "outcome_unknown": method == "PATCH",
                }
            return result
        except Exception:
            return {"error": "Calendar request outcome unknown", "outcome_unknown": True}
