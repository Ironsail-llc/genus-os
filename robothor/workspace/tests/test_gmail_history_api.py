"""The gws calls behind Gmail push: profile, history.list, metadata get, watch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from robothor.engine.tools.handlers import gws
from robothor.workspace.errors import NotFound, WorkspaceError
from robothor.workspace.google.adapter import GoogleMailSync
from robothor.workspace.ingest.google import GwsGmailHistory


class Recorder:
    def __init__(self, reply: Any = None) -> None:
        self.calls: list[list[str]] = []
        self.reply = reply if reply is not None else {"ok": True}

    def __call__(self, args: list[str], timeout: int = 30) -> Any:
        self.calls.append(list(args))
        return self.reply


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(gws, "_run_gws", r)
    return r


def _params(argv: list[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(argv[argv.index("--params") + 1])
    return loaded


def test_profile(rec: Recorder) -> None:
    GoogleMailSync().get_profile()
    assert rec.calls[0][:3] == ["gmail", "users", "getProfile"]
    assert _params(rec.calls[0]) == {"userId": "me"}


def test_history_list_asks_for_inbox_additions(rec: Recorder) -> None:
    GoogleMailSync().list_history("123")
    argv = rec.calls[0]
    assert argv[:4] == ["gmail", "users", "history", "list"]
    assert _params(argv) == {
        "userId": "me",
        "startHistoryId": "123",
        "historyTypes": "messageAdded",
        "labelId": "INBOX",
    }


def test_history_list_follows_a_page_token(rec: Recorder) -> None:
    GoogleMailSync().list_history("123", page_token="tok-2")
    assert _params(rec.calls[0])["pageToken"] == "tok-2"


def test_watch_sends_topic_and_labels_in_the_body(rec: Recorder) -> None:
    GoogleMailSync().watch("projects/example-project/topics/gmail", label_ids=["INBOX"])
    argv = rec.calls[0]
    assert argv[:3] == ["gmail", "users", "watch"]
    assert _params(argv) == {"userId": "me"}
    body = json.loads(argv[argv.index("--json") + 1])
    assert body == {
        "topicName": "projects/example-project/topics/gmail",
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }


async def test_async_api_returns_answers(rec: Recorder) -> None:
    rec.reply = {"emailAddress": "agent@example.com", "historyId": "9"}
    api = GwsGmailHistory()
    assert (await api.profile())["historyId"] == "9"
    await api.message("m1")
    assert rec.calls[-1][:4] == ["gmail", "users", "messages", "get"]
    assert _params(rec.calls[-1])["format"] == "metadata"


@pytest.mark.parametrize(
    "error",
    [
        '{"error": {"code": 404, "message": "Requested entity was not found."}}',
        "HTTP 404 Not Found",
    ],
)
async def test_a_404_is_not_found(rec: Recorder, error: str) -> None:
    rec.reply = {"error": error, "hint": "not_found"}
    with pytest.raises(NotFound):
        await GwsGmailHistory().history("1")


async def test_any_other_error_is_a_workspace_error(rec: Recorder) -> None:
    rec.reply = {"error": "gws command timed out after 30s", "hint": "timeout"}
    with pytest.raises(WorkspaceError) as caught:
        await GwsGmailHistory().history("1")
    assert not isinstance(caught.value, NotFound)
