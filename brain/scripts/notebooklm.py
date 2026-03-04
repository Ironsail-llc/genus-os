#!/usr/bin/env python3
"""
NotebookLM Enterprise API Helper

Usage:
    python notebooklm.py create "My Notebook"
    python notebooklm.py list
    python notebooklm.py get <notebook_id>
    python notebooklm.py delete <notebook_id>
    python notebooklm.py add-text <notebook_id> "Title" "Content..."
    python notebooklm.py add-url <notebook_id> "https://example.com"
    python notebooklm.py share <notebook_id> user@email.com [PROJECT_ROLE_WRITER|PROJECT_ROLE_READER]
"""

import json
import os
import sys
from pathlib import Path

# Setup path for imports
sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "memory_system"
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
    ),
)

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Configuration
TOKEN_FILE = os.path.expanduser("~/.moltbot/notebooklm-token.json")
PROJECT_NUMBER = "152250299895"
LOCATION = "us"  # License is in US region
BASE_URL = f"https://{LOCATION}-discoveryengine.googleapis.com/v1alpha"


def get_credentials():
    """Load and refresh OAuth credentials."""
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )

    if creds.expired or not creds.valid:
        creds.refresh(Request())
        # Save refreshed token
        with open(TOKEN_FILE, "w") as f:
            json.dump(
                {
                    "token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": list(creds.scopes),
                },
                f,
                indent=2,
            )

    return creds


def get_headers():
    """Get request headers with auth token."""
    creds = get_credentials()
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def create_notebook(title: str) -> dict:
    """Create a new notebook."""
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks"
    resp = requests.post(url, headers=get_headers(), json={"title": title})
    resp.raise_for_status()
    return resp.json()


def list_notebooks() -> dict:
    """List recently viewed notebooks."""
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks:listRecentlyViewed"
    resp = requests.get(url, headers=get_headers())
    resp.raise_for_status()
    return resp.json()


def get_notebook(notebook_id: str) -> dict:
    """Get a specific notebook."""
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks/{notebook_id}"
    resp = requests.get(url, headers=get_headers())
    resp.raise_for_status()
    return resp.json()


def delete_notebook(notebook_id: str) -> dict:
    """Delete a notebook."""
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks/{notebook_id}"
    resp = requests.delete(url, headers=get_headers())
    resp.raise_for_status()
    return {"deleted": notebook_id}


def add_text_source(notebook_id: str, title: str, content: str) -> dict:
    """Add a text source to a notebook."""
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks/{notebook_id}/sources:batchCreate"
    body = {"userContents": [{"textContent": {"sourceName": title, "content": content}}]}
    resp = requests.post(url, headers=get_headers(), json=body)
    resp.raise_for_status()
    return resp.json()


def add_url_source(notebook_id: str, url_to_add: str, title: str = None) -> dict:
    """Add a web URL source to a notebook."""
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks/{notebook_id}/sources:batchCreate"
    body = {
        "userContents": [{"webContent": {"url": url_to_add, "sourceName": title or url_to_add}}]
    }
    resp = requests.post(url, headers=get_headers(), json=body)
    resp.raise_for_status()
    return resp.json()


def share_notebook(
    notebook_id: str, email: str, role: str = "PROJECT_ROLE_WRITER", notify: bool = True
) -> dict:
    """Share a notebook with another user.

    Roles: PROJECT_ROLE_WRITER (editor), PROJECT_ROLE_READER (viewer)
    """
    url = f"{BASE_URL}/projects/{PROJECT_NUMBER}/locations/{LOCATION}/notebooks/{notebook_id}:share"
    body = {"accountAndRoles": [{"email": email, "role": role}], "notifyViaEmail": notify}
    resp = requests.post(url, headers=get_headers(), json=body)
    resp.raise_for_status()
    return {"shared": email, "role": role}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    try:
        if cmd == "create":
            title = sys.argv[2] if len(sys.argv) > 2 else "Untitled Notebook"
            result = create_notebook(title)
            print(json.dumps(result, indent=2))

        elif cmd == "list":
            result = list_notebooks()
            print(json.dumps(result, indent=2))

        elif cmd == "get":
            notebook_id = sys.argv[2]
            result = get_notebook(notebook_id)
            print(json.dumps(result, indent=2))

        elif cmd == "delete":
            notebook_id = sys.argv[2]
            result = delete_notebook(notebook_id)
            print(json.dumps(result, indent=2))

        elif cmd == "add-text":
            notebook_id = sys.argv[2]
            title = sys.argv[3]
            content = sys.argv[4]
            result = add_text_source(notebook_id, title, content)
            print(json.dumps(result, indent=2))

        elif cmd == "add-url":
            notebook_id = sys.argv[2]
            url_to_add = sys.argv[3]
            title = sys.argv[4] if len(sys.argv) > 4 else None
            result = add_url_source(notebook_id, url_to_add, title)
            print(json.dumps(result, indent=2))

        elif cmd == "share":
            notebook_id = sys.argv[2]
            email = sys.argv[3]
            role = sys.argv[4] if len(sys.argv) > 4 else "PROJECT_ROLE_WRITER"
            result = share_notebook(notebook_id, email, role)
            print(json.dumps(result, indent=2))

        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
            sys.exit(1)

    except requests.exceptions.HTTPError as e:
        print(f"API Error: {e.response.status_code}")
        print(e.response.text)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
