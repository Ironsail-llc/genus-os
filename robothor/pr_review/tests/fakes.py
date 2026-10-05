"""In-process fakes for GitHub, Google Chat and the CRM task sink."""

from __future__ import annotations

from typing import Any

from robothor.pr_review.tasks import ReviewTaskSpec, task_body

REPO = "acme/widgets"


def make_pr(
    number: int = 7,
    head: str = "a" * 40,
    *,
    repo: str = REPO,
    draft: bool = False,
    state: str = "open",
    author: str = "alice",
    labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "title": f"Change {number}",
        "body": "",
        "user": {"login": author},
        "labels": [{"name": label} for label in labels],
        "head": {"sha": head, "ref": "feature"},
        "base": {"sha": "0" * 40, "ref": "main"},
        "html_url": f"https://github.com/{repo}/pull/{number}",
    }


class FakeGitHub:
    def __init__(self) -> None:
        self.prs: dict[tuple[str, int], dict[str, Any]] = {}
        self.files: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.commits: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.requested: list[tuple[str, int]] = []
        self.compare: dict[tuple[str, str], str] = {}
        self.fail_list: set[str] = set()

    def add(self, pr: dict[str, Any], repo: str = REPO, files: list | None = None) -> None:
        self.prs[(repo, pr["number"])] = pr
        self.files[(repo, pr["number"])] = files or [
            {"filename": "src/a.py", "additions": 80, "deletions": 5, "patch": "@@ -1 +1 @@\n+x"}
        ]

    async def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        if repo in self.fail_list:
            raise RuntimeError("boom")
        return [p for (r, _), p in self.prs.items() if r == repo and p["state"] == "open"]

    async def review_requested(self, login: str) -> list[tuple[str, int]]:
        return list(self.requested)

    async def get_pr(self, repo: str, number: int) -> dict[str, Any] | None:
        return self.prs.get((repo, number))

    async def list_files(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.files.get((repo, number), [])

    async def list_commits(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self.commits.get((repo, number), [])

    async def compare_status(self, repo: str, base: str, head: str) -> str:
        return self.compare.get((base, head), "ahead")


class FakeChat:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.reactions: list[tuple[str, str]] = []
        self.unreactions: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str, str]] = []
        self.list_calls: list[str] = []
        self._n = 0

    def post(
        self,
        text: str,
        *,
        sender: str = "users/alice",
        thread: str = "spaces/AAAA/threads/t1",
        reply: bool = False,
        time: str = "2026-10-05T10:00:00.000000Z",
        **extra: Any,
    ) -> dict[str, Any]:
        self._n += 1
        msg = {
            "name": f"spaces/AAAA/messages/m{self._n}",
            "text": text,
            "sender": {"name": sender, "type": "HUMAN"},
            "thread": {"name": thread},
            "threadReply": reply,
            "createTime": time,
            **extra,
        }
        self.messages.append(msg)
        return msg

    async def list_messages(self, space: str, since: str) -> list[dict[str, Any]]:
        self.list_calls.append(since)
        return [m for m in self.messages if m["createTime"] > since]

    async def react(self, message: str, emoji: str) -> bool:
        self.reactions.append((message, emoji))
        return True

    async def unreact(self, message: str, emoji: str, self_users: tuple[str, ...] = ()) -> bool:
        self.unreactions.append((message, emoji))
        return True

    async def reply(self, space: str, thread: str, text: str) -> str:
        self.replies.append((space, thread, text))
        return f"{space}/messages/reply{len(self.replies)}"


class FakeTasks:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.by_key: dict[str, str] = {}
        self.reopened: list[str] = []

    async def reopen(self, task_id: str) -> None:
        self.reopened.append(task_id)

    async def create(self, spec: ReviewTaskSpec) -> str:
        if spec.dedup_value in self.by_key:
            return self.by_key[spec.dedup_value]
        task_id = f"task-{len(self.created) + 1}"
        self.by_key[spec.dedup_value] = task_id
        self.created.append(
            {"id": task_id, "title": spec.title, "body": task_body(spec), "spec": spec}
        )
        return task_id
