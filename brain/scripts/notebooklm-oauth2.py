#!/usr/bin/env python3
"""OAuth flow with local server callback"""

import json
import os
from pathlib import Path

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRET = os.path.expanduser("~/.config/gog/client_secret.json")
TOKEN_FILE = os.path.expanduser("~/.moltbot/notebooklm-token.json")

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
]

print("Starting OAuth flow with local server on port 8080...")
print("A browser window should open. Sign in as robothor@ironsail.ai")

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)

# This will start a local server and open browser
creds = flow.run_local_server(
    port=8080,
    authorization_prompt_message="Opening browser for authorization...",
    success_message="Authorization complete! You can close this window.",
    open_browser=False,  # We'll print the URL instead
)

Path(TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
with open(TOKEN_FILE, "w") as f:
    json.dump(
        {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
        },
        f,
        indent=2,
    )

print(f"\n✅ Token saved to {TOKEN_FILE}")
print(f"Access token: {creds.token[:50]}...")
