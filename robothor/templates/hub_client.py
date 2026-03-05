"""
Hub client — API client for programmaticresources.com.

Calls the programmaticresources.com API to search, download, and publish
agent template bundles.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
from pathlib import Path

import httpx

HUB_BASE_URL = os.getenv("PROGRAMMATIC_RESOURCES_URL", "https://programmaticresources.com")
DEFAULT_TIMEOUT = 30.0


class HubError(Exception):
    """Base error for hub operations."""


class HubAuthError(HubError):
    """Raised when authentication fails."""


class HubClient:
    """API client for the Programmatic Resources hub."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.base_url = (base_url or HUB_BASE_URL).rstrip("/")
        self.api_key = (
            api_key or os.getenv("PROGRAMMATIC_RESOURCES_API_KEY") or self._load_api_key()
        )
        self._client: httpx.Client | None = None

    def _load_api_key(self) -> str | None:
        """Load API key from robothor config."""
        config_path = Path.home() / "robothor" / "config.yaml"
        if not config_path.exists():
            return None
        try:
            import yaml

            config = yaml.safe_load(config_path.read_text()) or {}
            return config.get("instance", {}).get("api_key")
        except Exception:
            return None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {"User-Agent": "robothor-cli/1.0"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        return self._client

    def fetch_registry(self) -> dict:
        """Fetch all bundles from the hub.

        Returns dict mapping agent_id -> {slug, name, description, version, ...}
        """
        resp = self.client.get("/api/bundles")
        resp.raise_for_status()
        bundles = resp.json()
        return {b["slug"]: b for b in bundles}

    def search(self, query: str, department: str | None = None) -> list[dict]:
        """Search the hub for agents matching a query.

        Returns list of bundle dicts.
        """
        params: dict[str, str] = {}
        if query:
            params["q"] = query
        if department:
            params["department"] = department
        resp = self.client.get("/api/bundles", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_bundle(self, slug: str) -> dict | None:
        """Get a single bundle by slug."""
        resp = self.client.get(f"/api/bundles/{slug}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def download_bundle(self, slug: str, dest_dir: str | None = None) -> Path:
        """Download a bundle tarball and extract it.

        Returns path to the extracted bundle directory.
        """
        resp = self.client.get(
            f"/api/bundles/{slug}/download",
            follow_redirects=True,
        )
        if resp.status_code == 401:
            raise HubAuthError(
                "Authentication required. Set API key with:\n"
                "  robothor config set api-key pr_xxxxxxxxxxxx"
            )
        if resp.status_code == 402:
            data = resp.json()
            raise HubError(
                f"Purchase required for '{slug}' "
                f"(${data.get('price_cents', 0) / 100:.0f}). "
                f"Buy at: {self.base_url}/bundle/{slug}"
            )
        resp.raise_for_status()

        # Write to temp file and extract
        dest = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="pr-"))
        tarball_path = dest / f"{slug}.tar.gz"
        tarball_path.write_bytes(resp.content)

        if tarfile.is_tarfile(tarball_path):
            with tarfile.open(tarball_path, "r:gz") as tf:
                tf.extractall(dest, filter="data")
            tarball_path.unlink()

        # Find the extracted directory (GitHub tarballs have a top-level dir)
        subdirs = [d for d in dest.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            return subdirs[0]
        return dest

    def submit(self, repo_url: str) -> dict:
        """Submit a GitHub repo to the hub catalog.

        Returns the created bundle metadata.
        """
        resp = self.client.post("/api/submit", json={"repoUrl": repo_url})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise HubError(data.get("error", "Submission failed"))
        return data.get("bundle", {})

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
