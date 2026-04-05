#!/usr/bin/env python3
"""OAuth flow for workspace-mcp credentials.

Starts a local HTTP server on port 8085 to handle the OAuth redirect.
If running on a remote server, set up SSH port forwarding first:
    ssh -L 8085:localhost:8085 your-server

Usage:
    python3 scripts/workspace-mcp-auth.py
"""

import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
]

CLIENT_CONFIG = {
    "installed": {
        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

PORT = 8085
CREDS_DIR = Path("~/.google_workspace_mcp/credentials").expanduser()
_ai_email = os.environ.get("ROBOTHOR_AI_EMAIL", "default")
CREDS_PATH = CREDS_DIR / f"{_ai_email}.json"


def main():
    flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, scopes=SCOPES)

    print(f"\nStarting OAuth flow on http://localhost:{PORT}")
    print("If on a remote server, ensure SSH port forwarding is active:")
    print(f"  ssh -L {PORT}:localhost:{PORT} your-server\n")

    # run_local_server handles everything: opens browser, catches redirect
    creds = flow.run_local_server(
        port=PORT,
        open_browser=False,
        authorization_prompt_message=(
            f"Go to this URL in a browser:\n\n"
            f"  {{url}}\n\n"
            f"Sign in as {_ai_email} and authorize.\n"
            f"Waiting for redirect on port {PORT}..."
        ),
        success_message="Authorization complete! You can close this tab.",
        login_hint=_ai_email,
        access_type="offline",
        prompt="consent",
    )

    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    creds_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else None,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }

    with CREDS_PATH.open("w") as f:
        json.dump(creds_data, f, indent=2)

    print(f"\nCredentials saved to {CREDS_PATH}")
    print("workspace-mcp is ready! Restart Claude Code to use the new tools.")


if __name__ == "__main__":
    main()
