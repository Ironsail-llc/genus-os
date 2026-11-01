"""A verified coding job's branch, pushed and opened as a draft pull request.

``claude_code_publish`` is the only way a job's work leaves the host: a job's own
shell may never push (:data:`robothor.engine.coding.sandbox.NEVER`). This
module pushes the commit that passed the job's verify to its ``genus/cc-*`` branch on
the clone's origin -- every fetch and push URL of which must be a
``ROBOTHOR_CODING_REMOTES`` entry -- and opens a **draft** pull
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


async def push_branch(
    repo: Path, branch: str, commit: str, remote: repos.RemoteSpec, token: str
) -> None:
    """Push the verified ``commit`` to ``origin``'s ``refs/heads/<branch>``.

    The refspec names the commit, not the branch, and a branch whose tip is no
    longer ``commit`` is refused: another job's shell can write ``genus/*``
    refs, so the tip at publish time may be code that never passed verify.
    Origin's push URLs are checked again under the clone lock. The token is in
    the environment only.
    """
    async with repos.clone_lock(repo):
        try:
            _, tip = await repos.git(
                repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
            )
            if tip != commit:
                raise PublishError(
                    f"branch {branch} moved since verify: it points at {tip}, but the verified "
                    f"commit is {commit}; start a new job for the new work"
                )
            if (await repos.origin_remote(repo)).key != remote.key:
                raise PublishError(f"{repo}'s origin changed during publish")
            await repos.git(
                repo, "push", "--quiet", "origin", f"{commit}:refs/heads/{branch}",
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
