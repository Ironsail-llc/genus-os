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

from pathlib import Path
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


# ── the exec half of I6 ──────────────────────────────────────────────────────
#
# `read_file` and `send_file` both ask `attachments.is_inbox_secret`, which
# resolves against the workspace. `exec` asks neither: it gates on
# `secret_paths.exec_reads_secret`, which gates on `is_secret_path`. So while
# the two tools above refused, `cat`, `head`, `grep` and `python3 -c` printed
# the operator's own `.env` straight into a tool result — hence into the
# model's context and `agent_run_steps`. The reviewer's canary probe ran it for
# real and read `sk-proj-…` back out of the exec stdout.
#
# Held here rather than in `test_secret_exec.py` because it is this finding:
# the same file, the last door left open.

CANARY = "sk-proj-CANARYVALUE-0001"


class TestExecCannotPrintIt:
    @pytest.mark.parametrize(
        "template",
        [
            "cat {p}",
            "head -5 {p}",
            "grep KEY {p}",
            "python3 -c \"print(open('{p}').read())\"",
            "cd /tmp && tail -n +1 {p}",
        ],
    )
    def test_the_command_is_refused(self, tmp_path, template) -> None:
        from robothor.engine.secret_paths import exec_reads_secret

        row = saved(tmp_path, ".env", body=f"OPENAI_API_KEY={CANARY}\n".encode())
        command = template.format(p=row["path"])
        assert exec_reads_secret(command) is not None, f"{command!r} was allowed"

    @pytest.mark.parametrize(
        "spelling",
        [
            "{d}/secret/../secret/{n}",
            "{d}/./secret/{n}",
            "{d}//secret//{n}",
            "{d}/secret/../../{date}/secret/{n}",
        ],
    )
    def test_no_spelling_of_the_same_file_walks_past_the_rule(self, tmp_path, spelling) -> None:
        """M8. The rule matches a directory SEQUENCE and `PurePath` does not
        resolve `..`, so `secret/../secret/<file>` named the same bytes and was
        allowed — the one spelling where the inbox copy was protected LESS than
        a `.env`, whose rule matches a basename that `..` cannot hide. The
        reviewer's probe printed the canary through it."""
        from robothor.engine.secret_paths import exec_reads_secret

        row = saved(tmp_path, ".env", body=f"OPENAI_API_KEY={CANARY}\n".encode())
        stored = Path(row["path"])
        date = stored.parent.parent.name
        command = "cat " + spelling.format(d=stored.parent.parent, n=stored.name, date=date)
        assert exec_reads_secret(command) is not None, f"{command!r} was allowed"

    @pytest.mark.asyncio
    async def test_a_real_exec_child_cannot_bounce_through_dotdot(
        self, tmp_path, monkeypatch
    ) -> None:
        """The bounce really read the bytes, so the refusal is asserted the same
        way — a real subprocess, and the canary nowhere in the result."""
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.filesystem import HANDLERS

        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
        stored = Path(saved(tmp_path, ".env", body=f"OPENAI_API_KEY={CANARY}\n".encode())["path"])

        result = await HANDLERS["exec"](
            {"command": f"cat {stored.parent}/../secret/{stored.name}", "timeout": 10},
            ToolContext(agent_id="worker", workspace=str(tmp_path)),
        )
        assert "error" in result
        assert CANARY not in str(result)

    def test_the_stored_path_is_a_secret_path(self, tmp_path) -> None:
        """The predicate underneath, so the refusal does not depend on which
        reader an agent reaches for next."""
        from robothor.engine.secret_paths import is_secret_path

        row = saved(tmp_path, "credentials.json", uid="BQAD-77")
        assert is_secret_path(row["path"])

    @pytest.mark.asyncio
    async def test_a_real_exec_child_never_prints_the_canary(self, tmp_path, monkeypatch) -> None:
        """A real subprocess under a throwaway workspace, because a refusal that
        only exists in the predicate is a refusal nobody has watched work."""
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.filesystem import HANDLERS

        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
        row = saved(tmp_path, ".env", body=f"OPENAI_API_KEY={CANARY}\n".encode())

        result = await HANDLERS["exec"](
            {"command": f"cat {row['path']}", "timeout": 10},
            ToolContext(agent_id="worker", workspace=str(tmp_path)),
        )
        assert "error" in result
        assert "stdout" not in result
        assert CANARY not in str(result)

    def test_copying_it_out_is_left_to_the_send_gate(self, tmp_path) -> None:
        """`exec_reads_secret` does not object to `cp` — not for this file and
        not for a `.env` on disk either; it gates on the commands that PRINT.
        Moving the bytes without reading them is closed one door further on, by
        `attachment_gate.refuse_to_send`, which scans what `send_file` is about
        to hand the channel. Asserted so the shape of the guarantee is on the
        record rather than assumed: this is where the copy-out stops."""
        from robothor.engine.attachment_gate import refuse_to_send
        from robothor.engine.secret_paths import exec_reads_secret

        row = saved(tmp_path, ".env", body=f"OPENAI_API_KEY={CANARY}\n".encode())
        copy = tmp_path / "report.txt"
        copy.write_bytes(Path(row["path"]).read_bytes())

        assert exec_reads_secret(f"cp {row['path']} {copy}") is None
        assert refuse_to_send(copy, tmp_path) is not None


class TestExecIsNotOtherwiseNarrowed:
    """The rule is a shape, and a shape can over-reach. These are the paths the
    last two reviews found, asserted through `exec` rather than through the
    predicate."""

    def test_an_ordinary_inbox_file_can_still_be_read(self, tmp_path) -> None:
        from robothor.engine.secret_paths import exec_reads_secret

        row = saved(tmp_path, "notes.txt", body=b"nothing secret here")
        assert exec_reads_secret(f"cat {row['path']}") is None

    def test_a_project_path_that_looks_like_the_inbox_is_unaffected(self, tmp_path) -> None:
        """R3 again: `<workspace>/projects/inbox/secret/design.md` is an
        ordinary file and `cat` must keep working on it."""
        from robothor.engine.secret_paths import exec_reads_secret

        design = tmp_path / "projects" / "inbox" / "secret" / "design.md"
        design.parent.mkdir(parents=True)
        design.write_text("the Q4 roadmap")
        assert exec_reads_secret(f"cat {design}") is None
        assert exec_reads_secret(f"cat {design.parent}/./{design.name}") is None

    def test_dotdot_out_of_the_quarantine_names_an_ordinary_file(self, tmp_path) -> None:
        """M8's normalisation must not invent a match. A `..` that walks OUT of
        `secret/` names the sibling the operator sent in the clear."""
        from robothor.engine.secret_paths import exec_reads_secret

        row = saved(tmp_path, "notes.txt", body=b"nothing secret here")
        stored = Path(row["path"])
        sibling = f"{stored.parent}/secret/../{stored.name}"
        assert exec_reads_secret(f"cat {sibling}") is None

    @pytest.mark.asyncio
    async def test_a_real_exec_child_still_reads_an_ordinary_inbox_file(
        self, tmp_path, monkeypatch
    ) -> None:
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.filesystem import HANDLERS

        monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("ROBOTHOR_EXEC_ENV_MODE", "enforce")
        row = saved(tmp_path, "notes.txt", body=b"nothing secret here")

        result = await HANDLERS["exec"](
            {"command": f"cat {row['path']}", "timeout": 10},
            ToolContext(agent_id="worker", workspace=str(tmp_path)),
        )
        assert "nothing secret here" in str(result.get("stdout", ""))
