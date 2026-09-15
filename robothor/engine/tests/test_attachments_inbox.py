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
