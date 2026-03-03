"""
Hub client — GitHub API client for the Agent Hub (programmaticresources.com).

Stubbed for now. When the hub launches, fill in these methods with httpx calls
to the GitHub API. The CLI "just works" with remote sources once these are
implemented.
"""

from __future__ import annotations


class HubNotConfiguredError(Exception):
    """Raised when hub operations are attempted before hub is ready."""


class HubClient:
    """GitHub API client for the Robothor Agent Hub."""

    def __init__(self, org: str = "programmatic-resources"):
        self.org = org

    def fetch_registry(self) -> dict:
        """Fetch the agent registry from the hub.

        Returns dict mapping agent_id -> {version, description, repo, ...}
        """
        raise HubNotConfiguredError(
            "Hub not configured yet. Use local templates.\n"
            "  Install from local: robothor agent install templates/agents/<dept>/<id>/\n"
            "  Import existing:    robothor agent import <id>"
        )

    def download_release(self, repo: str, version: str = "latest") -> str:
        """Download a specific agent release from GitHub.

        Returns path to the downloaded and extracted bundle.
        """
        raise HubNotConfiguredError(
            f"Hub not configured yet. Use local templates.\n  Tried to download: {repo}@{version}"
        )

    def search(self, query: str) -> list[dict]:
        """Search the hub for agents matching a query.

        Returns list of {id, name, description, version, repo} dicts.
        """
        raise HubNotConfiguredError(
            f"Hub not configured yet. Use local templates.\n  Searched for: {query}"
        )

    def publish(self, bundle_path: str, repo: str) -> dict:
        """Publish a template bundle to the hub.

        Returns {url, version, status}.
        """
        raise HubNotConfiguredError(
            f"Hub not configured yet.\n  Tried to publish: {bundle_path} -> {repo}"
        )
