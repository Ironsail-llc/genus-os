#!/usr/bin/env python3
"""OAuth flow for NotebookLM API with robothor@ironsail.ai"""

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Scopes for NotebookLM
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
]

CLIENT_SECRET = os.path.expanduser("~/.config/gog/client_secret.json")
TOKEN_FILE = os.path.expanduser("~/.moltbot/notebooklm-token.json")


def main():
    creds = None

    # Check for existing token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            print("Loaded existing token")

    # If no valid credentials, run OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing expired token...")
            creds.refresh(Request())
        else:
            print("Starting OAuth flow...")
            print(f"Using client: {CLIENT_SECRET}")

            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET, SCOPES, redirect_uri="http://localhost:8080/"
            )

            # Use console flow for headless server
            auth_url, _ = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
                login_hint="robothor@ironsail.ai",
            )

            print("\n" + "=" * 60)
            print("Please visit this URL to authorize:")
            print(auth_url)
            print("=" * 60 + "\n")

            # Get the authorization code
            code = input("Enter the authorization code: ").strip()

            flow.fetch_token(code=code)
            creds = flow.credentials

        # Save credentials
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
        print(f"Token saved to {TOKEN_FILE}")

    print(f"\nToken valid: {creds.valid}")
    print(f"Token expired: {creds.expired}")
    if creds.token:
        print(f"Access token: {creds.token[:50]}...")

    return creds


if __name__ == "__main__":
    main()
