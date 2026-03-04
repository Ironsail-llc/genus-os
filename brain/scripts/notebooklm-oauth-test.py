#!/usr/bin/env python3
"""Test NotebookLM API with OAuth (user credentials instead of service account)"""

import json
import os
import pickle

from google.auth.transport.requests import Request

# Scopes needed for NotebookLM
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# OAuth client credentials (from robothor project)
CLIENT_CONFIG = {
    "installed": {
        "client_id": "152250299895-vqvt2i5vq5gs0q4qlvhm7q46qg5vbi5p.apps.googleusercontent.com",
        "client_secret": "GOCSPX-placeholder",  # Need to get this
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

TOKEN_FILE = os.path.expanduser("~/.moltbot/notebooklm-oauth-token.pickle")


def get_credentials():
    creds = None

    # Check for existing token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token:
            creds = pickle.load(token)

    # If no valid credentials, need to authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("Need to run OAuth flow - requires browser interaction")
            return None

    return creds


def main():
    # First, let's check if gog's OAuth tokens might work
    gog_token_path = os.path.expanduser("~/.config/gog/tokens/robothor@ironsail.ai.json")

    if os.path.exists(gog_token_path):
        print(f"Found gog token at: {gog_token_path}")
        with open(gog_token_path) as f:
            token_data = json.load(f)
            print(f"Token keys: {list(token_data.keys())}")
            if "scope" in token_data:
                print(f"Scopes: {token_data.get('scope', 'N/A')}")
    else:
        print("No gog token found")

    # Check for any existing cloud credentials
    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if os.path.exists(adc_path):
        print(f"\nFound ADC at: {adc_path}")
        with open(adc_path) as f:
            adc_data = json.load(f)
            print(f"ADC type: {adc_data.get('type', 'unknown')}")
            if "client_id" in adc_data:
                print(f"Client ID: {adc_data.get('client_id', 'N/A')[:30]}...")


if __name__ == "__main__":
    main()
