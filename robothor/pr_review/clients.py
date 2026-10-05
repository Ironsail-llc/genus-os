"""The two outside systems the suite talks to, behind small async interfaces.

* :class:`GitHubClient` — pull requests, files, commits, compare, and the
  review-requested search. HTTP through ``github_api._client`` and the
  vault-first token, the same path every GitHub tool uses.
* :class:`GwsChat` — Google Chat through the ``gws`` CLI wrapper
  (``robothor.engine.tools.handlers.gws.run_gws``): list a space's messages
  since a time, react, reply in a thread. No model is involved.

Tests replace both with fakes; nothing here is called from a test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol
from urllib.parse import quote

__all__ = ["ChatClient", "GitHubClient", "GitHubPort", "GwsChat"]


class GitHubPort(Protocol):
    async def list_open_prs(self, repo: str) -> list[dict[str, Any]]: ...
    async def review_requested(self, login: str) -> list[tuple[str, int]]: ...
    async def get_pr(self, repo: str, number: int) -> dict[str, Any] | None: ...
    async def list_files(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    async def list_commits(self, repo: str, number: int) -> list[dict[str, Any]]: ...
    async def compare_status(self, repo: str, base: str, head: str) -> str: ...


class ChatClient(Protocol):
    async def list_messages(self, space: str, since: str) -> list[dict[str, Any]]: ...
    async def react(self, message: str, emoji: str) -> bool: ...
    async def reply(self, space: str, thread: str, text: str) -> str: ...


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    """Read-only GitHub calls for the intake and the review tools."""

    def __init__(self, token: str | None = None) -> None:
        from robothor.engine.tools.handlers import github_api

        self._api = github_api
        self._token = token if token is not None else github_api._get_token()
        if not self._token:
            raise GitHubError("GITHUB_TOKEN not configured")

    @property
    def _base(self) -> str:
        return str(self._api._GITHUB_API)

    def _headers(self) -> dict[str, str]:
        return dict(self._api._headers(self._token))

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._api._client(30.0) as client:
            resp = await client.get(f"{self._base}{path}", headers=self._headers(), params=params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def _pages(self, path: str, params: dict[str, Any], max_pages: int) -> list[Any]:
        async with self._api._client(30.0) as client:
            return list(
                await self._api._paginate(
                    client, f"{self._base}{path}", self._headers(), params, max_pages=max_pages
                )
            )

    async def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return await self._pages(
            f"/repos/{repo}/pulls", {"state": "open", "per_page": 100}, max_pages=3
        )

    async def review_requested(self, login: str) -> list[tuple[str, int]]:
        data = await self._get(
            "/search/issues",
            {"q": f"is:pr is:open archived:false review-requested:{login}", "per_page": 100},
        )
        out: list[tuple[str, int]] = []
        for item in (data or {}).get("items") or []:
            repo_url = str(item.get("repository_url") or "")
            if "/repos/" in repo_url and item.get("number"):
                out.append((repo_url.split("/repos/", 1)[1], int(item["number"])))
        return out

    async def get_pr(self, repo: str, number: int) -> dict[str, Any] | None:
        data = await self._get(f"/repos/{repo}/pulls/{number}")
        return data if isinstance(data, dict) else None

    async def list_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._pages(f"/repos/{repo}/pulls/{number}/files", {"per_page": 100}, 30)

    async def list_commits(self, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._pages(f"/repos/{repo}/pulls/{number}/commits", {"per_page": 100}, 3)

    async def compare_status(self, repo: str, base: str, head: str) -> str:
        data = await self._get(
            f"/repos/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        )
        if data is None:
            return "missing"
        return str(data.get("status") or "")


class GwsChat:
    """Google Chat through the gws CLI. Every call is a blocking subprocess, so threaded."""

    async def _run(self, args: list[str]) -> dict[str, Any]:
        from robothor.engine.tools.handlers.gws import run_gws

        result = await asyncio.to_thread(run_gws, args)
        return result if isinstance(result, dict) else {"output": result}

    async def list_messages(self, space: str, since: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        token = ""
        for _ in range(10):
            params: dict[str, Any] = {
                "parent": space,
                "pageSize": 100,
                "filter": f'createTime > "{since}"',
                "orderBy": "createTime asc",
            }
            if token:
                params["pageToken"] = token
            data = await self._run(
                ["chat", "spaces", "messages", "list", "--params", json.dumps(params)]
            )
            if data.get("error"):
                raise RuntimeError(f"gws chat list failed: {data['error']}")
            messages.extend(m for m in data.get("messages") or [] if isinstance(m, dict))
            token = str(data.get("nextPageToken") or "")
            if not token:
                break
        return messages

    async def react(self, message: str, emoji: str) -> bool:
        if not emoji or not message:
            return False
        data = await self._run(
            [
                "chat",
                "spaces",
                "messages",
                "reactions",
                "create",
                "--params",
                json.dumps({"parent": message}),
                "--json",
                json.dumps({"emoji": {"unicode": emoji}}),
            ]
        )
        return not data.get("error")

    async def reply(self, space: str, thread: str, text: str) -> str:
        data = await self._run(
            [
                "chat",
                "spaces",
                "messages",
                "create",
                "--params",
                json.dumps(
                    {"parent": space, "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"}
                ),
                "--json",
                json.dumps({"text": text, "thread": {"name": thread}}),
            ]
        )
        if data.get("error"):
            raise RuntimeError(f"gws chat reply failed: {data['error']}")
        return str(data.get("name") or "")
