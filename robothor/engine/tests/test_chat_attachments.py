"""Files attached to a webchat turn reach the agent the way Telegram's do.

The dashboard posts ``attachments: [{name, mime, size, data}]`` (base64) beside
the message. The engine checks the same limits the browser states (five
megabytes a file, three a turn, a short list of types), keeps each file in the
inbox under ``inbox/webchat``, and hands the agent the operator's words plus
the structured note ``format_attachment_note`` writes for Telegram. A picture
also goes to the model as an image block, but only when the registry says the
model can take one.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine import attachments
from robothor.engine.chat import _sessions, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType

#: The smallest file that is really a PNG: the magic number is what is checked.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

REQUEST_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def mock_runner(engine_config):
    runner = MagicMock()
    runner.config = engine_config
    runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.COMPLETED, output_text="Got it.", trigger_type=TriggerType.WEBCHAT
        )
    )
    return runner


@pytest.fixture
async def client(engine_config, mock_runner):
    from fastapi import FastAPI

    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(mock_runner, engine_config)
    app.include_router(router)
    with patch("robothor.engine.chat.save_exchange_async", new=AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    _sessions.clear()


def _file(name: str, mime: str, data: bytes) -> dict:
    return {
        "name": name,
        "mime": mime,
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


async def _send(client: AsyncClient, message: str, files: list[dict]):
    return await client.post(
        "/chat/send",
        json={"message": message, "request_id": REQUEST_ID, "attachments": files},
    )


def _inbox(engine_config) -> list:
    root = engine_config.workspace / "inbox" / "webchat"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


class TestKeptInTheInbox:
    @pytest.mark.asyncio
    async def test_a_text_file_lands_under_inbox_webchat(self, client, engine_config) -> None:
        res = await _send(client, "what is in here?", [_file("notes.md", "text/markdown", b"# hi")])
        assert res.status_code == 200
        files = _inbox(engine_config)
        assert len(files) == 1
        assert files[0].read_bytes() == b"# hi"
        assert files[0].name.endswith("-notes.md")
        assert oct(files[0].stat().st_mode & 0o777) == "0o600"

    @pytest.mark.asyncio
    async def test_the_turn_is_the_message_then_the_telegram_note(
        self, client, engine_config, mock_runner
    ) -> None:
        await _send(client, "what is in here?", [_file("notes.md", "text/markdown", b"# hi")])
        sent = mock_runner.execute.call_args.kwargs["message"]
        path = str(_inbox(engine_config)[0])
        assert sent.startswith("what is in here?")
        assert "[1 file attached — kept on disk; use the paths below]" in sent
        assert f"path: {path}" in sent
        assert "--- begin notes.md ---" in sent
        assert "# hi" in sent

    @pytest.mark.asyncio
    async def test_an_image_is_named_with_view_image_guidance(
        self, client, engine_config, mock_runner
    ) -> None:
        await _send(client, "look", [_file("shot.png", "image/png", PNG)])
        sent = mock_runner.execute.call_args.kwargs["message"]
        path = str(_inbox(engine_config)[0])
        assert f"call view_image on {path} to look at it" in sent

    @pytest.mark.asyncio
    async def test_a_file_with_no_message_still_goes(self, client, mock_runner) -> None:
        res = await _send(client, "", [_file("data.csv", "text/csv", b"a,b\n1,2\n")])
        assert res.status_code == 200
        sent = mock_runner.execute.call_args.kwargs["message"]
        assert "The operator sent this with no message." in sent


class TestImageBlocks:
    @pytest.mark.asyncio
    async def test_an_image_goes_as_a_block_when_the_model_accepts_images(
        self, client, mock_runner
    ) -> None:
        with patch("robothor.engine.model_registry.image_capability", return_value="accepts"):
            await _send(client, "look", [_file("shot.png", "image/png", PNG)])
        blocks = mock_runner.execute.call_args.kwargs.get("user_images")
        assert blocks == [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
            }
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", ["rejects", "unknown"])
    async def test_no_block_unless_the_model_accepts(self, client, mock_runner, answer) -> None:
        with patch("robothor.engine.model_registry.image_capability", return_value=answer):
            await _send(client, "look", [_file("shot.png", "image/png", PNG)])
        assert not mock_runner.execute.call_args.kwargs.get("user_images")
        assert "call view_image on" in mock_runner.execute.call_args.kwargs["message"]

    @pytest.mark.asyncio
    async def test_a_text_file_is_never_a_block(self, client, mock_runner) -> None:
        with patch("robothor.engine.model_registry.image_capability", return_value="accepts"):
            await _send(client, "read", [_file("a.txt", "text/plain", b"hello")])
        assert not mock_runner.execute.call_args.kwargs.get("user_images")


class TestRefused:
    @pytest.mark.asyncio
    async def test_a_file_over_five_megabytes_is_413(self, client, engine_config, mock_runner):
        big = b"a" * (5 * 1024 * 1024 + 1)
        res = await _send(client, "here", [_file("big.txt", "text/plain", big)])
        assert res.status_code == 413
        assert "5 MB" in res.json()["error"]
        mock_runner.execute.assert_not_called()
        assert _inbox(engine_config) == []

    @pytest.mark.asyncio
    async def test_more_than_three_files_is_413(self, client, engine_config, mock_runner):
        files = [_file(f"{i}.txt", "text/plain", b"x") for i in range(4)]
        res = await _send(client, "here", files)
        assert res.status_code == 413
        mock_runner.execute.assert_not_called()
        assert _inbox(engine_config) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "item",
        [
            _file("archive.zip", "application/zip", b"PK\x03\x04"),
            _file("run.sh", "application/x-sh", b"echo hi"),
        ],
    )
    async def test_an_unsupported_type_is_422(self, client, engine_config, mock_runner, item):
        res = await _send(client, "here", [item])
        assert res.status_code == 422
        mock_runner.execute.assert_not_called()
        assert _inbox(engine_config) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "item",
        [
            {"name": "a.txt", "mime": "text/plain", "size": 3, "data": "not base64!!"},
            {"name": "a.txt", "mime": "text/plain", "size": 99, "data": "aGk="},
            {"mime": "text/plain", "size": 2, "data": "aGk="},
            _file("fake.png", "image/png", b"this is not a picture"),
            "a string, not a file",
        ],
    )
    async def test_a_malformed_attachment_is_422(self, client, engine_config, mock_runner, item):
        res = await _send(client, "here", [item])
        assert res.status_code == 422
        mock_runner.execute.assert_not_called()
        assert _inbox(engine_config) == []


class TestCredentials:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("name", "mime"), [(".env", "text/plain"), ("credentials.json", "application/json")]
    )
    async def test_a_credential_named_file_is_quarantined_under_secret(
        self, client, engine_config, mock_runner, name, mime
    ) -> None:
        await _send(client, "keep this", [_file(name, mime, b'{"token": "abc123secretvalue"}')])
        files = _inbox(engine_config)
        assert len(files) == 1
        assert files[0].parent.name == attachments.SECRET_SUBDIR
        sent = mock_runner.execute.call_args.kwargs["message"]
        assert attachments.SECRET_FILE_NOTE in sent
        assert "abc123secretvalue" not in sent


class TestHistory:
    @pytest.mark.asyncio
    async def test_history_names_the_attachments(self, client) -> None:
        await _send(client, "look", [_file("shot.png", "image/png", PNG)])
        res = await client.get("/chat/history")
        users = [m for m in res.json()["messages"] if m["role"] == "user"]
        assert users, "the turn was recorded"
        rows = users[-1]["extras"]["attachments"]
        assert [(r["name"], r["kind"], r["size"]) for r in rows] == [
            ("shot.png", "image", len(PNG))
        ]

    @pytest.mark.asyncio
    async def test_the_persisted_turn_carries_the_rows(self, engine_config, mock_runner) -> None:
        from fastapi import FastAPI

        _sessions.clear()
        app = FastAPI()
        with patch("robothor.engine.chat.load_all_sessions", return_value={}):
            init_chat(mock_runner, engine_config)
        app.include_router(router)
        saved = AsyncMock()
        with patch("robothor.engine.chat.save_exchange_async", new=saved):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                await _send(c, "look", [_file("a.txt", "text/plain", b"hi")])
        _sessions.clear()
        extras = saved.call_args.kwargs["user_extras"]
        assert [r["name"] for r in extras["attachments"]] == ["a.txt"]

    @pytest.mark.asyncio
    async def test_a_plain_turn_is_unchanged(self, client, mock_runner) -> None:
        await client.post("/chat/send", json={"message": "hi"})
        assert mock_runner.execute.call_args.kwargs["message"] == "hi"
        assert not mock_runner.execute.call_args.kwargs.get("user_images")
        res = await client.get("/chat/history")
        assert "extras" not in res.json()["messages"][0]


class TestSessionStart:
    def test_user_images_become_blocks_on_the_user_turn(self) -> None:
        from robothor.engine.session import AgentSession

        block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        session = AgentSession("test-agent")
        session.start("sys", "look at this", [], user_images=[block])
        assert session.messages[-1] == {
            "role": "user",
            "content": [{"type": "text", "text": "look at this"}, block],
        }

    def test_no_images_keeps_the_plain_string_turn(self) -> None:
        from robothor.engine.session import AgentSession

        session = AgentSession("test-agent")
        session.start("sys", "hello", [])
        assert session.messages[-1] == {"role": "user", "content": "hello"}


class TestFailures:
    """A file that cannot be kept is an answer, not a 500 (the Telegram rule)."""

    @pytest.mark.asyncio
    async def test_a_failed_save_answers_json_and_runs_nothing(
        self, client, engine_config, mock_runner
    ) -> None:
        with patch("robothor.engine.attachments.save_attachment", side_effect=OSError("disk full")):
            res = await _send(client, "here", [_file("a.txt", "text/plain", b"hi")])
        assert res.status_code == 500
        assert res.json()["request_admitted"] is False
        assert "a.txt" in res.json()["error"]
        mock_runner.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_files_already_kept_for_a_failed_turn_are_removed(
        self, client, engine_config, mock_runner
    ) -> None:
        real = attachments.save_attachment
        calls = {"n": 0}

        def second_fails(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ValueError("attachment path escaped the inbox root")
            return real(**kwargs)

        with patch("robothor.engine.attachments.save_attachment", side_effect=second_fails):
            res = await _send(
                client,
                "here",
                [_file("a.txt", "text/plain", b"one"), _file("b.txt", "text/plain", b"two")],
            )
        assert res.status_code == 500
        assert _inbox(engine_config) == []
        mock_runner.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_file_kept_by_an_earlier_turn_survives_a_failed_one(
        self, client, engine_config, mock_runner
    ) -> None:
        first = _file("a.txt", "text/plain", b"one")
        await _send(client, "keep", [first])
        kept = _inbox(engine_config)
        assert len(kept) == 1
        real = attachments.save_attachment
        calls = {"n": 0}

        def second_fails(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(**kwargs)

        with patch("robothor.engine.attachments.save_attachment", side_effect=second_fails):
            res = await _send(client, "again", [first, _file("b.txt", "text/plain", b"two")])
        assert res.status_code == 500
        assert _inbox(engine_config) == kept
        assert kept[0].read_bytes() == b"one"

    @pytest.mark.asyncio
    async def test_a_failed_extraction_still_sends_the_file(
        self, client, engine_config, mock_runner
    ) -> None:
        with patch(
            "robothor.engine.file_extract.describe_bytes", side_effect=ImportError("no pdf lib")
        ):
            res = await _send(client, "read", [_file("a.pdf", "application/pdf", b"%PDF-1.4")])
        assert res.status_code == 200
        sent = mock_runner.execute.call_args.kwargs["message"]
        assert f"path: {_inbox(engine_config)[0]}" in sent
        assert "note: " in sent


class TestJoinRunning:
    @pytest.mark.asyncio
    async def test_files_cannot_join_a_running_turn(self, client, engine_config, mock_runner):
        res = await client.post(
            "/chat/send",
            json={
                "message": "also this",
                "join_running": True,
                "attachments": [_file("a.txt", "text/plain", b"hi")],
            },
        )
        assert res.status_code == 422
        assert res.json()["request_admitted"] is False
        mock_runner.execute.assert_not_called()
        assert _inbox(engine_config) == []


class TestTextQuoting:
    @pytest.mark.asyncio
    async def test_binary_bytes_in_a_text_file_are_not_quoted(self, client, mock_runner) -> None:
        await _send(client, "read", [_file("a.txt", "text/plain", b"ab\x00\xff\xfe\x00cd")])
        sent = mock_runner.execute.call_args.kwargs["message"]
        assert "--- begin a.txt ---" not in sent
        assert "�" not in sent


class TestNames:
    @pytest.mark.asyncio
    async def test_the_original_name_is_kept_for_the_chip(self, client) -> None:
        await _send(client, "see", [_file("Résumé 2026.txt", "text/plain", b"hi")])
        res = await client.get("/chat/history")
        row = res.json()["messages"][0]["extras"]["attachments"][0]
        assert row["original_name"] == "Résumé 2026.txt"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("name", "mime", "data"),
        [
            ("../../x.png", "image/png", PNG),
            ("a\x00b.txt", "text/plain", b"hi"),
            ("/etc/passwd.txt", "text/plain", b"hi"),
            ("n" * 300 + ".txt", "text/plain", b"hi"),
        ],
    )
    async def test_a_hostile_name_stays_inside_the_inbox(
        self, client, engine_config, name, mime, data
    ) -> None:
        res = await _send(client, "here", [_file(name, mime, data)])
        assert res.status_code == 200
        root = (engine_config.workspace / "inbox" / "webchat").resolve()
        files = _inbox(engine_config)
        assert len(files) == 1
        assert root in files[0].resolve().parents
        assert "\x00" not in files[0].name
        assert len(files[0].name) <= attachments.MAX_NAME_CHARS + 65


class TestLimitsAgree:
    def test_the_dashboard_states_the_same_limits(self) -> None:
        """The composer's numbers and the engine's are one set, checked here."""
        import re
        from pathlib import Path

        from robothor.engine import chat_attachments

        ts = (
            Path(__file__).resolve().parents[3] / "app" / "src" / "lib" / "chat" / "attachments.ts"
        ).read_text()

        def number(name: str) -> int:
            expr = re.search(rf"export const {name} = ([0-9 *]+);", ts)
            assert expr, name
            value = 1
            for part in expr.group(1).split("*"):
                value *= int(part.strip())
            return value

        assert number("MAX_ATTACHMENTS") == chat_attachments.MAX_FILES
        assert number("MAX_ATTACHMENT_BYTES") == chat_attachments.MAX_FILE_BYTES
        block = ts.split("export const ACCEPTED_TYPES", 1)[1].split("};", 1)[0]
        assert set(re.findall(r'"([a-z]+/[a-z0-9.+-]+)"', block)) == set(
            chat_attachments.ACCEPTED_MIMES
        )
        assert set(re.findall(r'"(\.[a-z]+)"', block)) == set(chat_attachments.ACCEPTED_EXTENSIONS)


class TestActiveRequestPin:
    def test_a_turn_with_image_blocks_is_pinned_itself(self) -> None:
        from robothor.engine.compaction import ACTIVE_REQUEST_PIN, PIN_KEY
        from robothor.engine.context import _pin_active_request

        turn = {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this screenshot?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
        out = _pin_active_request(
            [{"role": "system", "content": "sys"}, turn], "what is in this screenshot?"
        )
        assert len(out) == 2
        assert out[1][PIN_KEY] == ACTIVE_REQUEST_PIN
        assert out[1]["content"] == turn["content"]


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 28


class TestExtensions:
    """The name's extension is part of the type: a declared MIME alone is a claim."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("name", "mime", "data"),
        [
            ("tool.exe", "text/plain", b"MZ not text"),
            ("evil.png", "text/plain", b"hello"),
            ("notes.txt", "image/png", PNG),
            ("report.pdf", "text/plain", b"hello"),
            ("blob", "text/plain", b"MZ\x00\x00binary"),
        ],
    )
    async def test_a_name_that_disagrees_with_its_type_is_422(
        self, client, engine_config, mock_runner, name, mime, data
    ) -> None:
        res = await _send(client, "here", [_file(name, mime, data)])
        assert res.status_code == 422
        assert res.json()["error"] == f"{name} is not a supported type"
        mock_runner.execute.assert_not_called()
        assert _inbox(engine_config) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("name", "mime", "data"),
        [
            ("notes.txt", "text/plain", b"hello"),
            ("photo.jpg", "image/jpeg", JPEG),
            ("Photo.JPEG", "image/jpeg", JPEG),
            ("notes.md", "text/plain", b"# hi"),
            ("README", "text/plain", b"plain words"),
            ("screenshot", "image/png", PNG),
        ],
    )
    async def test_a_name_that_agrees_with_its_type_is_kept(
        self, client, engine_config, name, mime, data
    ) -> None:
        res = await _send(client, "here", [_file(name, mime, data)])
        assert res.status_code == 200
        assert len(_inbox(engine_config)) == 1
