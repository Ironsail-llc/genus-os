"""A verified coding job's branch, pushed and opened as a draft pull request.

``coding_publish`` is the only way a job's work leaves the host: a job's own
shell may never push (:data:`robothor.engine.coding.sandbox.NEVER`). This
module pushes the job's ``genus/cc-*`` branch to the clone's origin -- which
must be a ``ROBOTHOR_CODING_REMOTES`` entry -- and opens a **draft** pull
request through the GitHub REST API (httpx; no ``gh`` binary). Nothing here
merges, approves or marks ready: a person reviews the draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from robothor.engine.coding import repos

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["GITHUB_API", "PublishError", "PublishedPr", "open_draft_pr", "push_branch"]

GITHUB_API = "https://api.github.com"
_TIMEOUT_S = 20.0


class PublishError(ValueError):
    """Why a job could not be published, said so the agent can act on it."""


@dataclass(frozen=True)
class PublishedPr:
    url: str
    number: int | None
    draft: bool


async def push_branch(repo: Path, branch: str, remote: repos.RemoteSpec, token: str) -> None:
    """``git push origin <branch>`` from ``repo``, the token in the environment only."""
    async with repos.clone_lock(repo):
        try:
            await repos.git(
                repo, "push", "--quiet", "origin", f"refs/heads/{branch}:refs/heads/{branch}",
                url=remote.url, token=token,
            )  # fmt: skip
        except repos.RepoError as exc:
            raise PublishError(str(exc)) from None


async def open_draft_pr(
    remote: repos.RemoteSpec, *, head: str, base: str, title: str, body: str, token: str
) -> PublishedPr:
    """One ``POST /repos/{owner}/{repo}/pulls`` with ``draft: true``."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "body": body, "head": head, "base": base, "draft": True}
    url = f"{GITHUB_API}/repos/{remote.owner}/{remote.name}/pulls"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise PublishError(f"GitHub request failed: {type(exc).__name__}") from None
    if resp.status_code not in (200, 201):
        message = ""
        try:
            data: Any = resp.json()
            message = str(data.get("message") or "") if isinstance(data, dict) else ""
        except ValueError:
            pass
        raise PublishError(
            f"GitHub refused the pull request ({resp.status_code})"
            + (f": {repos.redact(message, token)[:300]}" if message else "")
        )
    data = resp.json()
    return PublishedPr(
        url=str(data.get("html_url") or ""),
        number=data.get("number"),
        draft=bool(data.get("draft", True)),
    )
