"""
Hub client — API client for programmaticresources.com.

Calls the programmaticresources.com API to search, download, and publish
agent template bundles.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from robothor.engine.sanitize import sanitize_log
from robothor.templates.safety import (
    TemplateSecurityError,
    contained_path,
    safe_relative_path,
    trusted_directory,
    validate_identifier,
    validate_sha256,
)

logger = logging.getLogger("robothor.hub")

HUB_BASE_URL = os.getenv("PROGRAMMATIC_RESOURCES_URL", "https://programmaticresources.com")
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 512
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024


def _is_retryable(status_code: int) -> bool:
    """5xx errors and 429 are retryable; 4xx (except 429) are not."""
    return status_code >= 500 or status_code == 429


class HubError(Exception):
    """Base error for hub operations."""


class HubAuthError(HubError):
    """Raised when authentication fails."""


def trusted_bundle_sha256(metadata: object) -> str:
    """Extract the mandatory digest from trusted registry metadata."""

    if not isinstance(metadata, dict):
        raise HubError("Hub bundle metadata is missing a valid SHA-256 digest")
    try:
        return validate_sha256(metadata.get("sha256"), label="hub bundle SHA-256")
    except TemplateSecurityError as error:
        raise HubError("Hub bundle metadata is missing a valid SHA-256 digest") from error


def _safe_slug(value: object) -> str:
    try:
        return validate_identifier(value, label="hub bundle slug")
    except TemplateSecurityError as error:
        raise HubError(str(error)) from error


def _prepare_destination_root(value: str | Path) -> Path:
    """Create/validate an explicit download root without traversing symlinks."""

    requested = Path(value).expanduser().absolute()
    existing = requested
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            raise HubError("Bundle destination has no trusted existing parent")
        existing = parent

    try:
        trusted_ancestor = trusted_directory(existing, label="bundle destination ancestor")
        if requested == existing:
            return trusted_ancestor
        relative = requested.relative_to(existing).as_posix()
        destination = contained_path(
            trusted_ancestor,
            relative,
            label="bundle destination path",
        )
        destination.mkdir(parents=True, mode=0o700)
        return trusted_directory(destination, label="bundle destination")
    except (TemplateSecurityError, ValueError) as error:
        raise HubError("Bundle destination must be a trusted directory") from error


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
                logger.debug(
                    "Hub request: %s %s (attempt %d)",
                    sanitize_log(method),
                    sanitize_log(url),
                    attempt + 1,
                )
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
                        sanitize_log(method),
                        sanitize_log(url),
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
                    sanitize_log(method),
                    sanitize_log(url),
                    attempt + 1,
                    MAX_RETRIES,
                    e,
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE * 2**attempt)

        raise HubError(f"Request failed after {MAX_RETRIES} attempts: {last_exc}")

    @staticmethod
    def _safe_path(base: Path, candidate: str) -> Path:
        """Return ``base / <name>`` for a single trusted filename ``candidate``.

        ``candidate`` must already be a bare filename (callers pass ``path.name``
        / ``f"{slug}.tar.gz"``); any directory separator or traversal component
        is rejected (path-injection guard).
        """
        leaf = Path(candidate).name
        if leaf != candidate or leaf in ("", ".", ".."):
            raise HubError(f"unsafe path: {candidate}")
        # Resolve and confirm containment with the os.path realpath/commonpath
        # barrier (the form CodeQL recognises as a path-injection sanitizer).
        base_real = os.path.realpath(base)
        target_real = os.path.realpath(os.path.join(base_real, leaf))  # noqa: PTH118
        if os.path.commonpath([base_real, target_real]) != base_real:
            raise HubError(f"unsafe path: {candidate}")
        return Path(target_real)

    def _verify_checksum(self, content: bytes, expected_sha256: str) -> bool:
        """Verify the SHA-256 of the downloaded bytes.

        Hashes the in-memory response body directly (no file read) — both faster
        and free of any path-injection sink.
        """
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            logger.error(
                "Checksum mismatch: expected %s, got %s",
                sanitize_log(expected_sha256),
                sanitize_log(actual),
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
            try:
                slug = _safe_slug(b.get("slug"))
            except HubError:
                logger.warning("Skipping bundle with invalid slug")
                continue
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
        slug = _safe_slug(slug)
        resp = self._request_with_retry("GET", f"/api/bundles/{slug}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        raw_bundle = resp.json()
        if not isinstance(raw_bundle, dict):
            raise HubError("Hub returned malformed bundle metadata")
        returned_slug = _safe_slug(raw_bundle.get("slug"))
        if returned_slug != slug:
            raise HubError("Hub bundle metadata slug does not match the requested bundle")
        bundle: dict[str, Any] = raw_bundle
        return bundle

    def download_bundle(
        self,
        slug: str,
        *,
        expected_sha256: str | None = None,
        dest_dir: str | Path | None = None,
    ) -> Path:
        """Download and safely extract an integrity-pinned bundle tarball.

        Returns path to the extracted bundle directory.
        """
        slug = _safe_slug(slug)
        try:
            expected_sha256 = validate_sha256(expected_sha256, label="expected hub bundle SHA-256")
        except TemplateSecurityError as error:
            raise HubError("A valid trusted SHA-256 is required before download") from error

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

        content = resp.content
        if len(content) > MAX_DOWNLOAD_BYTES:
            raise HubError(f"Bundle '{slug}' exceeds the maximum download size")
        if not self._verify_checksum(content, expected_sha256):
            raise HubError(f"Checksum verification failed for bundle '{slug}'")

        # The slug is registry input and must never influence a filesystem
        # path.  tempfile supplies an unpredictable leaf beneath an explicit
        # caller-owned root (or the platform temporary root).
        destination: Path
        if dest_dir is None:
            destination = Path(tempfile.mkdtemp(prefix="robothor-hub-"))
        else:
            parent = _prepare_destination_root(dest_dir)
            destination = Path(tempfile.mkdtemp(prefix=".robothor-hub-", dir=parent))

        try:
            self._extract_archive(content, destination)
            root_setup = contained_path(destination, "setup.yaml", label="bundle setup path")
            if root_setup.is_file():
                return destination

            candidates = []
            for child in destination.iterdir():
                if not child.is_dir() or child.is_symlink():
                    continue
                setup = contained_path(child, "setup.yaml", label="bundle setup path")
                if setup.is_file():
                    candidates.append(child)
            if len(candidates) != 1:
                raise HubError("Downloaded bundle must contain exactly one setup.yaml root")
            return candidates[0]
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    @staticmethod
    def _extract_archive(content: bytes, destination: Path) -> None:
        """Extract only bounded regular files/directories under *destination*."""

        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                members = archive.getmembers()
                if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                    raise HubError("Bundle archive has an invalid number of members")

                seen: set[str] = set()
                total_size = 0
                checked: list[tuple[tarfile.TarInfo, Path]] = []
                for member in members:
                    try:
                        relative = safe_relative_path(member.name, label="archive member path")
                    except TemplateSecurityError as error:
                        raise HubError(f"Unsafe bundle archive member: {member.name!r}") from error
                    normalized = PurePosixPath(*relative.parts).as_posix()
                    if normalized in seen:
                        raise HubError(f"Duplicate bundle archive member: {normalized}")
                    seen.add(normalized)

                    if not (member.isdir() or member.isfile()):
                        raise HubError(f"Unsupported bundle archive member: {normalized}")
                    if member.size < 0:
                        raise HubError(f"Invalid bundle archive member size: {normalized}")
                    if member.isfile():
                        total_size += member.size
                        if total_size > MAX_EXTRACTED_BYTES:
                            raise HubError("Bundle archive exceeds the maximum extracted size")

                    target = contained_path(
                        destination, normalized, label="archive extraction path"
                    )
                    checked.append((member, target))

                for member, target in checked:
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        target.chmod(0o700)
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise HubError(f"Could not read archive member: {member.name}")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    if target.stat().st_size != member.size:
                        raise HubError(
                            f"Archive member size changed during extraction: {member.name}"
                        )
                    target.chmod(0o600)
        except (tarfile.TarError, OSError) as error:
            raise HubError(f"Invalid bundle archive: {error}") from error

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
