"""An in-memory Gmail mailbox speaking the history API the push ingestor reads."""

from __future__ import annotations

from typing import Any

from robothor.workspace.errors import NotFound

ME = "agent@example.com"


class FakeGmailHistory:
    """``profile`` / ``history`` / ``message`` / ``watch`` over a list of history records.

    Like Gmail, ``history(start)`` answers the records strictly after
    ``start`` and the mailbox's current ``historyId``; an expired start id is
    a 404 (:class:`NotFound`).
    """

    def __init__(self, history_id: int = 100, *, page_size: int = 2) -> None:
        self.history_id = history_id
        self.page_size = page_size
        self.records: list[tuple[int, dict[str, Any]]] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.expired = False
        self.history_calls: list[tuple[str, str | None]] = []
        self.message_calls: list[str] = []
        self.watches: list[tuple[str, list[str]]] = []
        self.watch_expiration = "1767225600000"

    def deliver(
        self,
        message_id: str,
        *,
        labels: tuple[str, ...] = ("INBOX", "UNREAD"),
        sender: str = "Alice <alice@example.com>",
        subject: str = "Hello",
    ) -> None:
        self.history_id += 1
        message = {
            "id": message_id,
            "threadId": f"t-{message_id}",
            "labelIds": list(labels),
            "snippet": "a short preview",
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": subject},
                    {"name": "Date", "value": "Thu, 1 Jan 2026 09:00:00 +0000"},
                ]
            },
        }
        self.messages[message_id] = message
        stub = {"id": message_id, "threadId": message["threadId"], "labelIds": list(labels)}
        self.records.append((self.history_id, stub))

    async def profile(self) -> dict[str, Any]:
        return {"emailAddress": ME, "historyId": str(self.history_id)}

    async def history(self, start_history_id: str, page_token: str | None = None) -> dict[str, Any]:
        self.history_calls.append((start_history_id, page_token))
        if self.expired:
            raise NotFound("Requested entity was not found.", status=404)
        after = [r for r in self.records if r[0] > int(start_history_id)]
        offset = int(page_token or 0)
        page = after[offset : offset + self.page_size]
        answer: dict[str, Any] = {"historyId": str(self.history_id)}
        if page:
            answer["history"] = [
                {"id": str(hid), "messagesAdded": [{"message": stub}]} for hid, stub in page
            ]
        if offset + self.page_size < len(after):
            answer["nextPageToken"] = str(offset + self.page_size)
        return answer

    async def message(self, message_id: str) -> dict[str, Any]:
        self.message_calls.append(message_id)
        if message_id not in self.messages:
            raise NotFound("Requested entity was not found.", status=404)
        return self.messages[message_id]

    async def watch(self, topic: str, label_ids: list[str]) -> dict[str, Any]:
        self.watches.append((topic, list(label_ids)))
        return {"historyId": str(self.history_id), "expiration": self.watch_expiration}
