"""``read_file`` must refuse the inbox copy of a credentials file.

Hostile review finding I6. `.env` sent over Telegram is saved as `<uid>-env`,
`credentials.json` and `id_ed25519` likewise lose what made them recognisable,
and ``secret_paths.is_secret_path`` — which gates ``read_file`` — never sees
the original name. Every one of them returned the file whole.

The first report claimed "the output redactor still catches values on the way
back". **It does not.** There is no redaction anywhere in
``robothor/engine/tools/dispatch.py``, and the redactor on the
``feat/vault-managed-secrets`` branch scrubs the assistant turn and the chat
content columns, not a tool result. The raw credential entered the model's
context, and a model that has read a value can paraphrase it past any output
filter.

The fix does not touch ``secret_paths`` — that module belongs to the vault
branch this week. The verdict is taken once at save time and recorded as the
DIRECTORY the file is kept in, and this is the single additive call site that
asks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from robothor.engine import attachments
from robothor.engine.tools.handlers import filesystem


async def read_file(path, workspace):
    ctx = MagicMock()
    ctx.workspace = str(workspace)
    return await filesystem.HANDLERS["read_file"]({"path": str(path)}, ctx)


def saved(tmp_path, name: str, uid: str = "AgACaaa", body: bytes = b"TOKEN=super-secret"):
    return attachments.save_attachment(
        chat_id="100200300",
        file_id="f",
        file_unique_id=uid,
        name=name,
        data=body,
        kind="document",
        mime="text/plain",
        workspace=tmp_path,
    )


class TestRefused:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("uid", ["AgACaaa", "AgAC-xQ", "BQAD-77", "a-b-c-d"])
    @pytest.mark.parametrize("name", [".env", "credentials.json", "id_ed25519", ".netrc"])
    async def test_no_name_or_uid_shape_lets_it_be_read(self, tmp_path, uid, name) -> None:
        row = saved(tmp_path, name, uid)
        out = await read_file(row["path"], tmp_path)
        assert "error" in out, f"{uid}/{name} was readable"
        assert "super-secret" not in str(out)

    @pytest.mark.asyncio
    async def test_the_refusal_is_the_secret_path_sentence(self, tmp_path) -> None:
        """The same wording `read_file` already uses for `.env` on disk, so the
        agent learns one rule rather than two."""
        row = saved(tmp_path, ".env")
        out = await read_file(row["path"], tmp_path)
        assert "secrets file" in out["error"]
        assert "Credentials are supplied to tools by the platform" in out["error"]

    @pytest.mark.asyncio
    async def test_the_file_is_still_on_disk(self, tmp_path) -> None:
        """Refusing to READ it is not deleting it — the operator sent it."""
        from pathlib import Path

        row = saved(tmp_path, ".env")
        assert Path(row["path"]).read_bytes() == b"TOKEN=super-secret"


class TestStillReadable:
    @pytest.mark.asyncio
    async def test_an_ordinary_inbox_file_is_readable(self, tmp_path) -> None:
        row = saved(tmp_path, "notes.txt", body=b"nothing secret here")
        out = await read_file(row["path"], tmp_path)
        assert out.get("content") == "nothing secret here"

    @pytest.mark.asyncio
    async def test_a_file_outside_any_inbox_is_unaffected(self, tmp_path) -> None:
        ordinary = tmp_path / "report.md"
        ordinary.write_text("quarterly numbers")
        out = await read_file(ordinary, tmp_path)
        assert out.get("content") == "quarterly numbers"

    @pytest.mark.asyncio
    async def test_a_directory_named_secret_outside_the_inbox_is_unaffected(self, tmp_path) -> None:
        """The predicate keys on the inbox tree, so an unrelated `secret/`
        directory elsewhere on the box does not start refusing reads."""
        elsewhere = tmp_path / "projects" / "secret" / "notes.md"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("a project code-named secret")
        out = await read_file(elsewhere, tmp_path)
        assert out.get("content") == "a project code-named secret"

    @pytest.mark.asyncio
    async def test_a_project_path_that_looks_like_the_inbox_is_unaffected(self, tmp_path) -> None:
        """Re-review R3: the looser `inbox` + `secret` match made
        `<workspace>/projects/inbox/secret/design.md` unreadable."""
        design = tmp_path / "projects" / "inbox" / "secret" / "design.md"
        design.parent.mkdir(parents=True)
        design.write_text("the Q4 roadmap")
        out = await read_file(design, tmp_path)
        assert out.get("content") == "the Q4 roadmap"
