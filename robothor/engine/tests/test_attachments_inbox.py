"""The inbox: an inbound file is KEPT, at a path the agent can use.

Before this, a document that reached Telegram was downloaded into memory,
reduced to text (or to ``[Binary file: name, N bytes]``) and the bytes were
dropped on the floor. The agent could never open it, forward it, convert it
or attach it to a task. These tests pin the half of the fix that does not
involve Telegram at all: where the file lands, what the recorded row says,
and what the agent is told about it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.engine import attachments


class TestSafeName:
    def test_strips_directory_traversal(self) -> None:
        assert attachments.safe_name("../../.ssh/id_rsa") == "id_rsa"

    def test_strips_absolute_path(self) -> None:
        assert attachments.safe_name("/etc/robothor/secrets.enc.json") == "secrets.enc.json"

    def test_keeps_an_ordinary_name(self) -> None:
        assert attachments.safe_name("Q3 report (final).pdf") == "Q3-report-final.pdf"

    def test_a_name_that_sanitises_to_nothing_gets_a_default(self) -> None:
        assert attachments.safe_name("../..") == "file"
        assert attachments.safe_name("") == "file"

    def test_windows_separators_are_separators_too(self) -> None:
        assert attachments.safe_name(r"..\..\.ssh\id_rsa") == "id_rsa"

    def test_long_names_are_capped_but_keep_their_suffix(self) -> None:
        name = attachments.safe_name("a" * 500 + ".pdf")
        assert len(name) <= attachments.MAX_NAME_CHARS
        assert name.endswith(".pdf")


class TestInboxPath:
    def test_layout_is_channel_chat_date(self, tmp_path) -> None:
        when = datetime(2026, 9, 15, 23, 50, tzinfo=UTC)
        path = attachments.inbox_path(
            chat_id="100200300",
            file_unique_id="AgACfake",
            name="notes.txt",
            workspace=tmp_path,
            when=when,
        )
        assert path == tmp_path / "inbox" / "telegram" / "100200300" / "2026-09-15" / (
            "AgACfake-notes.txt"
        )

    def test_a_hostile_name_cannot_escape_the_inbox(self, tmp_path) -> None:
        path = attachments.inbox_path(
            chat_id="100200300",
            file_unique_id="AgACfake",
            name="../../../../.ssh/id_rsa",
            workspace=tmp_path,
        )
        assert (tmp_path / "inbox").resolve() in path.resolve().parents

    def test_a_hostile_chat_id_cannot_escape_the_inbox(self, tmp_path) -> None:
        path = attachments.inbox_path(
            chat_id="../../etc",
            file_unique_id="x",
            name="a.txt",
            workspace=tmp_path,
        )
        assert (tmp_path / "inbox").resolve() in path.resolve().parents

    def test_a_group_chat_id_keeps_its_sign(self, tmp_path) -> None:
        path = attachments.inbox_path(
            chat_id="-1001234567",
            file_unique_id="x",
            name="a.txt",
            workspace=tmp_path,
        )
        assert path.parent.parent.name == "-1001234567"


class TestSaveAttachment:
    def test_row_shape(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="BQACfileid",
            file_unique_id="AgACuniq",
            name="notes.txt",
            data=b"hello",
            kind="document",
            mime="text/plain",
            caption="have a look",
            workspace=tmp_path,
        )
        assert row["kind"] == "document"
        assert row["mime"] == "text/plain"
        assert row["size"] == 5
        assert row["telegram_file_id"] == "BQACfileid"
        assert row["telegram_file_unique_id"] == "AgACuniq"
        assert row["caption"] == "have a look"
        assert row["name"] == "notes.txt"
        # No dimensions were supplied, so the keys are absent rather than null.
        assert "width" not in row
        assert "height" not in row

    def test_bytes_land_on_disk_owner_only(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name="notes.txt",
            data=b"hello",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        from pathlib import Path

        saved = Path(row["path"])
        assert saved.read_bytes() == b"hello"
        assert saved.stat().st_mode & 0o777 == 0o600

    def test_dimensions_ride_when_known(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name="photo.jpg",
            data=b"\xff\xd8\xff",
            kind="image",
            mime="image/jpeg",
            width=1280,
            height=720,
            workspace=tmp_path,
        )
        assert row["width"] == 1280
        assert row["height"] == 720

    def test_the_same_file_unique_id_is_not_stored_twice(self, tmp_path) -> None:
        first = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name="notes.txt",
            data=b"hello",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        second = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name="notes.txt",
            data=b"hello",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        assert first["path"] == second["path"]
        assert second["deduplicated"] is True

    def test_a_hostile_name_is_written_inside_the_inbox(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name="../../../../.ssh/id_rsa",
            data=b"not a key",
            kind="document",
            mime="application/octet-stream",
            workspace=tmp_path,
        )
        from pathlib import Path

        assert (tmp_path / "inbox").resolve() in Path(row["path"]).resolve().parents
        assert not (tmp_path.parent / ".ssh" / "id_rsa").exists()


class TestTooLarge:
    def test_the_refusal_names_the_real_limit(self) -> None:
        sentence = attachments.too_large_sentence(25 * 1024 * 1024, name="clip.mp4")
        assert "20 MB" in sentence
        assert "clip.mp4" in sentence
        assert "link" in sentence.lower()

    def test_the_ceiling_is_telegrams_own(self) -> None:
        assert attachments.MAX_DOWNLOAD_BYTES == 20 * 1024 * 1024

    def test_with_no_known_size_it_claims_only_the_bound(self) -> None:
        """Re-review R2. The download bound fires mid-transfer, so the only
        number available there is how far the transfer got. Passing it made a
        500 MB upload come back as "is 21 MB" — true of the transfer, false of
        the file."""
        sentence = attachments.too_large_sentence(name="clip.mp4")
        assert "is larger than 20 MB" in sentence
        assert "clip.mp4" in sentence
        assert "21 MB" not in sentence

    def test_a_known_size_is_still_stated_exactly(self) -> None:
        """The declared-size path knows the real number and should say it."""
        assert "25 MB" in attachments.too_large_sentence(25 * 1024 * 1024, name="clip.mp4")

    def test_both_forms_name_the_limit_and_offer_the_way_round(self) -> None:
        for sentence in (
            attachments.too_large_sentence(name="a.bin"),
            attachments.too_large_sentence(25 * 1024 * 1024, name="a.bin"),
        ):
            assert "up to 20 MB" in sentence
            assert "link" in sentence.lower()


class TestNote:
    def _row(self, **kw):
        row = {
            "path": "/w/inbox/telegram/100200300/2026-09-15/u-notes.txt",
            "name": "notes.txt",
            "kind": "document",
            "mime": "text/plain",
            "size": 5,
            "telegram_file_id": "f",
            "telegram_file_unique_id": "u",
            "caption": "",
        }
        row.update(kw)
        return row

    def test_the_caption_is_the_instruction_and_comes_first(self) -> None:
        note = attachments.format_attachment_note(
            "what's this?", [attachments.NotedAttachment(row=self._row())]
        )
        assert note.startswith("what's this?")

    def test_every_attachment_names_its_path_kind_and_size(self) -> None:
        note = attachments.format_attachment_note(
            "", [attachments.NotedAttachment(row=self._row())]
        )
        assert "/w/inbox/telegram/100200300/2026-09-15/u-notes.txt" in note
        assert "document" in note
        assert "5 B" in note

    def test_an_image_is_told_to_be_looked_at(self) -> None:
        row = self._row(kind="image", mime="image/jpeg", name="photo.jpg", width=8, height=9)
        note = attachments.format_attachment_note("", [attachments.NotedAttachment(row=row)])
        assert "view_image" in note
        assert "8x9" in note

    def test_extracted_text_rides_with_the_path_for_the_rest(self) -> None:
        note = attachments.format_attachment_note(
            "",
            [attachments.NotedAttachment(row=self._row(), text="line one", text_total_chars=9000)],
        )
        assert "line one" in note
        assert "read_file" in note
        assert "9000" in note

    def test_a_vision_description_is_labelled_as_the_fallback_it_is(self) -> None:
        row = self._row(kind="image", mime="image/jpeg")
        note = attachments.format_attachment_note(
            "", [attachments.NotedAttachment(row=row, vision="a whiteboard with a diagram")]
        )
        assert "a whiteboard with a diagram" in note
        assert "local vision model" in note

    def test_an_album_is_one_note_with_every_path(self) -> None:
        rows = [
            self._row(path=f"/w/inbox/telegram/100200300/2026-09-15/u{i}-p.jpg", kind="image")
            for i in range(3)
        ]
        note = attachments.format_attachment_note(
            "three shots", [attachments.NotedAttachment(row=r) for r in rows]
        )
        for i in range(3):
            assert f"u{i}-p.jpg" in note
        assert note.count("view_image") >= 1

    def test_a_caption_is_never_swallowed_by_the_vision_prompt(self) -> None:
        """The 2026-09-15 complaint: the caption WAS the VLM prompt, so the
        agent never saw the operator's own words as an instruction."""
        row = self._row(kind="image", caption="rotate this and send it back")
        note = attachments.format_attachment_note(
            "rotate this and send it back",
            [attachments.NotedAttachment(row=row, vision="a photo of a cat")],
        )
        assert note.index("rotate this and send it back") < note.index("a photo of a cat")


class TestPrune:
    def _make(self, tmp_path, *, days_old: int, name: str = "a.txt"):
        when = datetime.now(UTC) - timedelta(days=days_old)
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id=f"u{days_old}{name}",
            name=name,
            data=b"x",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
            when=when,
        )
        from pathlib import Path

        return Path(row["path"])

    def test_old_files_go_and_recent_ones_stay(self, tmp_path) -> None:
        old = self._make(tmp_path, days_old=40, name="old.txt")
        fresh = self._make(tmp_path, days_old=1, name="fresh.txt")
        removed = attachments.prune_inbox(retention_days=30, workspace=tmp_path)
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_nothing_outside_the_inbox_is_ever_touched(self, tmp_path) -> None:
        outside = tmp_path / "keepme.txt"
        outside.write_text("mine now")
        self._make(tmp_path, days_old=99, name="old.txt")
        attachments.prune_inbox(retention_days=30, workspace=tmp_path)
        assert outside.exists()

    def test_zero_retention_prunes_nothing(self, tmp_path) -> None:
        old = self._make(tmp_path, days_old=999, name="old.txt")
        assert attachments.prune_inbox(retention_days=0, workspace=tmp_path) == 0
        assert old.exists()

    def test_a_missing_inbox_is_not_an_error(self, tmp_path) -> None:
        assert attachments.prune_inbox(retention_days=30, workspace=tmp_path / "nope") == 0

    def test_it_prunes_every_channel_by_default(self, tmp_path) -> None:
        """Round-1 M2. The default was `channel="telegram"`, so a file under
        `inbox/slack/` survived forever while both the docs and the setting's
        own help text promised `<workspace>/inbox/`."""
        from pathlib import Path

        old = datetime.now(UTC) - timedelta(days=40)
        paths = [
            Path(
                attachments.save_attachment(
                    chat_id="100200300",
                    file_id="f",
                    file_unique_id=f"u{channel}",
                    name="a.txt",
                    data=b"x",
                    kind="document",
                    mime="text/plain",
                    workspace=tmp_path,
                    channel=channel,
                    when=old,
                )["path"]
            )
            for channel in ("telegram", "slack", "webchat")
        ]
        assert attachments.prune_inbox(retention_days=30, workspace=tmp_path) == 3
        assert not any(path.exists() for path in paths)

    def test_naming_a_channel_still_prunes_only_that_one(self, tmp_path) -> None:
        from pathlib import Path

        old = datetime.now(UTC) - timedelta(days=40)
        kept = Path(
            attachments.save_attachment(
                chat_id="100200300",
                file_id="f",
                file_unique_id="uslack",
                name="a.txt",
                data=b"x",
                kind="document",
                mime="text/plain",
                workspace=tmp_path,
                channel="slack",
                when=old,
            )["path"]
        )
        attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="utg",
            name="a.txt",
            data=b"x",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
            channel="telegram",
            when=old,
        )
        assert (
            attachments.prune_inbox(retention_days=30, workspace=tmp_path, channel="telegram") == 1
        )
        assert kept.exists()

    def test_nothing_above_the_inbox_is_touched_by_the_wider_sweep(self, tmp_path) -> None:
        outside = tmp_path / "keepme.txt"
        outside.write_text("mine")
        attachments.prune_inbox(retention_days=30, workspace=tmp_path)
        assert outside.exists()


class TestSecretsTheOperatorSent:
    """Sanitising a filename destroys what ``secret_paths`` matches on.

    ``.env`` becomes ``env``; ``.ssh/id_rsa`` becomes ``id_rsa``. A file that
    was refused on the way in would then be readable, quotable into a prompt,
    and sendable back out under a name the rules no longer recognise —
    ``credentials.json`` worst of all, because ``.json`` is an extractable
    suffix and the whole file would otherwise be decoded into the first turn.

    The file is still KEPT. The operator sent it deliberately and may want it
    moved or renamed. What is refused is putting its contents in front of a
    model.
    """

    def test_the_original_name_is_what_decides(self) -> None:
        assert attachments.holds_credentials(".env") is True
        assert attachments.holds_credentials("../../.ssh/id_rsa") is True
        assert attachments.holds_credentials("credentials.json") is True
        assert attachments.holds_credentials("notes.txt") is False
        assert attachments.holds_credentials("env.example") is False

    def test_the_row_carries_the_verdict(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name="credentials.json",
            data=b'{"token": "abc"}',
            kind="document",
            mime="application/json",
            workspace=tmp_path,
        )
        assert row["secret"] is True

    def test_an_ordinary_file_is_not_marked(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u2",
            name="notes.txt",
            data=b"hello",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        assert "secret" not in row

    def test_it_is_still_saved(self, tmp_path) -> None:
        from pathlib import Path

        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name=".env",
            data=b"TOKEN=abc",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        assert Path(row["path"]).read_bytes() == b"TOKEN=abc"

    @pytest.mark.parametrize(
        "uid",
        ["AgACaaa", "AgAC-xQ", "BQAD-77", "CXYZ-11", "-leading", "trailing-", "a-b-c-d"],
    )
    @pytest.mark.parametrize("name", [".env", "credentials.json", "id_ed25519"])
    def test_a_dash_in_the_file_unique_id_cannot_defeat_the_gate(self, tmp_path, uid, name) -> None:
        """Hostile review I1. The verdict used to be re-derived by splitting the
        stored name on the FIRST dash — and Telegram's file_unique_id is
        URL-safe base64, whose alphabet contains one. `BQAD-77-credentials.json`
        was read as `77-credentials.json`, matched nothing, and an OAuth
        client-secret JSON was sent in full."""
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id=uid,
            name=name,
            data=b"secret-value-here",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        assert row["secret"] is True
        assert attachments.is_inbox_secret(row["path"], workspace=tmp_path) is True, row["path"]

    def test_the_verdict_is_the_directory_not_the_filename(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="BQAD-77",
            name="credentials.json",
            data=b"{}",
            kind="document",
            mime="application/json",
            workspace=tmp_path,
        )
        from pathlib import Path

        assert Path(row["path"]).parent.name == attachments.SECRET_SUBDIR
        assert row["original_name"] == "credentials.json"

    def test_an_ordinary_file_is_not_in_the_secret_directory(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="AgAC-xQ",
            name="notes.txt",
            data=b"hello",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        assert attachments.is_inbox_secret(row["path"], workspace=tmp_path) is False
        assert "original_name" not in row

    def test_a_path_outside_any_inbox_is_never_a_secret(self, tmp_path) -> None:
        """The predicate keys on the inbox tree, so an unrelated directory
        called `secret` elsewhere on the box does not answer for it."""
        assert (
            attachments.is_inbox_secret(tmp_path / "secret" / "a.txt", workspace=tmp_path) is False
        )

    def test_a_workspace_path_that_merely_looks_like_the_inbox_is_not_one(self, tmp_path) -> None:
        """Re-review R3. Matching "some `inbox` component followed by some
        `secret` one" made an ordinary project directory unsendable and
        unreadable."""
        design = tmp_path / "projects" / "inbox" / "secret" / "design.md"
        design.parent.mkdir(parents=True)
        design.write_text("the Q4 roadmap")
        assert attachments.is_inbox_secret(design, workspace=tmp_path) is False

    def test_the_real_inbox_secret_directory_still_matches(self, tmp_path) -> None:
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="AgAC-xQ",
            name=".env",
            data=b"TOKEN=abc",
            kind="document",
            mime="text/plain",
            workspace=tmp_path,
        )
        assert attachments.is_inbox_secret(row["path"], workspace=tmp_path) is True

    def test_another_instances_inbox_does_not_answer_for_this_one(self, tmp_path) -> None:
        """Anchored on THIS workspace: a path under a different tree's inbox is
        outside the root and is judged by containment, not by this predicate."""
        other = tmp_path / "other-instance"
        row = attachments.save_attachment(
            chat_id="100200300",
            file_id="f",
            file_unique_id="u",
            name=".env",
            data=b"TOKEN=abc",
            kind="document",
            mime="text/plain",
            workspace=other,
        )
        assert attachments.is_inbox_secret(row["path"], workspace=tmp_path / "mine") is False

    def test_the_note_says_kept_but_not_read_and_quotes_nothing(self) -> None:
        row = {
            "path": "/w/inbox/telegram/100200300/2026-09-15/u-env",
            "name": "env",
            "kind": "document",
            "mime": "text/plain",
            "size": 9,
            "secret": True,
        }
        note = attachments.format_attachment_note(
            "here you go",
            [attachments.NotedAttachment(row=row, text="TOKEN=abc", text_total_chars=9)],
        )
        assert "kept, but not read" in note
        assert "TOKEN=abc" not in note, "an extract must never survive the secret verdict"
        assert row["path"] in note, "the operator can still be told where it went"


class TestLooksLikeAnImage:
    """Round-1 M5: the MIME type and the name are claims, not evidence."""

    @pytest.mark.parametrize(
        "magic",
        [
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff\xe0",
            b"GIF89a",
            b"BM\x00\x00",
            b"II*\x00",
            b"MM\x00*",
            b"RIFF\x00\x00\x00\x00WEBP",
            b"\x00\x00\x00\x18ftypavif",
            b"\x00\x00\x00\x18ftypheic",
        ],
    )
    def test_real_image_bytes_are_recognised(self, magic) -> None:
        assert attachments.looks_like_an_image(magic + b"\x00" * 64) is True

    @pytest.mark.parametrize(
        "body",
        [
            b"a plain text file that happens to be called notes.png",
            b"%PDF-1.7",
            b"PK\x03\x04",
            b"\x00\x00\x00\x18ftypmp42",
            b"",
        ],
    )
    def test_everything_else_is_not(self, body) -> None:
        assert attachments.looks_like_an_image(body) is False


class TestHumanSize:
    def test_a_size_never_reads_as_the_limit_it_exceeds(self) -> None:
        """Round-1 M7. Rounding up made a 50.001 MB file render "50 MB", so the
        refusal read "is 50 MB, over the 50 MB a chat attachment may be"."""
        assert attachments.human_size(50 * 1024 * 1024 + 200_000) == "50.1 MB"

    def test_an_exact_limit_reads_as_a_whole_number(self) -> None:
        assert attachments.human_size(50 * 1024 * 1024) == "50 MB"
        assert attachments.human_size(20 * 1024 * 1024) == "20 MB"


class TestKindForMessageParts:
    @pytest.mark.parametrize(
        ("mime", "name", "expected"),
        [
            ("image/png", "a.png", "image"),
            ("application/pdf", "a.pdf", "document"),
            ("video/mp4", "a.mp4", "video"),
            ("audio/ogg", "a.ogg", "audio"),
            ("", "a.jpeg", "image"),
            ("", "a.bin", "document"),
        ],
    )
    def test_kind_from_mime_then_extension(self, mime, name, expected) -> None:
        assert attachments.kind_for(mime, name) == expected


class TestExtractable:
    def test_text_extensions_extract(self) -> None:
        assert attachments.extractable(".txt", "text/plain") == "text"

    def test_pdf_extracts(self) -> None:
        assert attachments.extractable(".pdf", "application/pdf") == "pdf"

    def test_a_zip_does_not(self) -> None:
        assert attachments.extractable(".zip", "application/zip") is None
