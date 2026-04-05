"""
Hub client — API client for programmaticresources.com.

Calls the programmaticresources.com API to search, download, and publish
agent template bundles.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("robothor.hub")

HUB_BASE_URL = os.getenv("PROGRAMMATIC_RESOURCES_URL", "https://programmaticresources.com")
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds


def _is_retryable(status_code: int) -> bool:
    """5xx errors and 429 are retryable; 4xx (except 429) are not."""
    return status_code >= 500 or status_code == 429


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
        config_path = (
            Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
            / "config.yaml"
        )
        if not config_path.exists():
            return None
        try:
            import yaml

            config = yaml.safe_load(config_path.read_text()) or {}
            result: str | None = config.get("instance", {}).get("api_key")
            return result
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

    def _request_with_retry(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Execute HTTP request with retry logic for transient failures."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                logger.debug("Hub request: %s %s (attempt %d)", method, url, attempt + 1)
                resp = self.client.request(method, url, **kwargs)

                # Handle rate limiting
                if resp.status_code == 429:
                    retry_after = min(
                        float(resp.headers.get("Retry-After", RETRY_BACKOFF_BASE * 2**attempt)),
                        60.0,
                    )
                    logger.warning("Rate limited, waiting %.1fs", retry_after)
                    time.sleep(retry_after)
                    continue

                # Don't retry client errors (except 429)
                if 400 <= resp.status_code < 500:
                    return resp

                # Retry server errors
                if resp.status_code >= 500:
                    logger.warning(
                        "Server error %d on %s %s (attempt %d/%d)",
                        resp.status_code,
                        method,
                        url,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF_BASE * 2**attempt)
                        continue

                return resp
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as e:
                last_exc = e
                logger.warning(
                    "Connection error on %s %s (attempt %d/%d): %s",
                    method,
                    url,
                    attempt + 1,
                    MAX_RETRIES,
                    e,
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE * 2**attempt)

        raise HubError(f"Request failed after {MAX_RETRIES} attempts: {last_exc}")

    def _verify_checksum(self, path: Path, expected_sha256: str) -> bool:
        """Verify file checksum if provided."""
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_sha256:
            logger.error(
                "Checksum mismatch for %s: expected %s, got %s",
                path.name,
                expected_sha256,
                actual,
            )
            return False
        return True

    def fetch_registry(self) -> dict[str, Any]:
        """Fetch all bundles from the hub.

        Returns dict mapping agent_id -> {slug, name, description, version, ...}
        """
        resp = self._request_with_retry("GET", "/api/bundles")
        resp.raise_for_status()
        bundles = resp.json()

        # Validate response structure
        registry: dict[str, Any] = {}
        for b in bundles:
            if not isinstance(b, dict):
                logger.warning("Skipping malformed bundle entry: %s", type(b).__name__)
                continue
            slug = b.get("slug")
            name = b.get("name")
            if not slug or not name:
                logger.warning("Skipping bundle missing slug/name: %s", b)
                continue
            registry[slug] = b

        return registry

    def search(self, query: str, department: str | None = None) -> list[dict[str, Any]]:
        """Search the hub for agents matching a query.

        Returns list of bundle dicts.
        """
        params: dict[str, str] = {}
        if query:
            params["q"] = query
        if department:
            params["department"] = department
        resp = self._request_with_retry("GET", "/api/bundles", params=params)
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json()
        return result

    def get_bundle(self, slug: str) -> dict[str, Any] | None:
        """Get a single bundle by slug."""
        resp = self._request_with_retry("GET", f"/api/bundles/{slug}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        bundle: dict[str, Any] = resp.json()
        return bundle

    def download_bundle(
        self, slug: str, dest_dir: str | None = None, expected_sha256: str | None = None
    ) -> Path:
        """Download a bundle tarball and extract it.

        Returns path to the extracted bundle directory.
        """
        resp = self._request_with_retry(
            "GET",
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

        # Verify checksum if provided
        if expected_sha256 and not self._verify_checksum(tarball_path, expected_sha256):
            tarball_path.unlink()
            raise HubError(f"Checksum verification failed for bundle '{slug}'")

        if tarfile.is_tarfile(tarball_path):
            with tarfile.open(tarball_path, "r:gz") as tf:
                tf.extractall(dest, filter="data")
            tarball_path.unlink()

        # Find the extracted directory (GitHub tarballs have a top-level dir)
        subdirs = [d for d in dest.iterdir() if d.is_dir()]

        # Validate extracted bundle
        bundle_dir = subdirs[0] if len(subdirs) == 1 else dest
        if not (bundle_dir / "setup.yaml").exists():
            logger.warning("Downloaded bundle '%s' missing setup.yaml", slug)

        if len(subdirs) == 1:
            return subdirs[0]
        return dest

    def submit(self, repo_url: str) -> dict[str, Any]:
        """Submit a GitHub repo to the hub catalog.

        Returns the created bundle metadata.
        """
        resp = self._request_with_retry("POST", "/api/submit", json={"repoUrl": repo_url})
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise HubError(data.get("error", "Submission failed"))
        bundle: dict[str, Any] = data.get("bundle", {})
        return bundle

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> HubClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
