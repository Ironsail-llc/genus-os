"""The refusal ladder is one function, and it runs at the moment bytes are read.

Hostile review 2026-09-15, finding C1. ``send_file`` checked containment,
``secret_paths``, the credential scan and the size ceiling — and then, for a
scheduled run, put the PATH in a queue. ``delivery_attachments.send_attachments``
took the path back out and handed it to the channel, which reads the bytes at
send time. Nothing was re-checked. The reviewer's probe:

    the agent wrote report.csv with "month,total", called send_file (queued —
    delivery.mode: announce has no live surface), then overwrote the file with
    AWS_SECRET_ACCESS_KEY=… and deliver() shipped the secret.

``run.attachments`` was worse still: those paths had never been checked at all.

So the ladder lives here, both callers use it, and a queued entry is pinned to
the digest it was approved with — the same function, asked twice, which is what
this change already argues for about ``scan_secret_literals``.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime by the helper below

from robothor.engine import attachment_gate as gate


def make(root: Path, name: str, content: bytes = b"hello") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestTheLadderIsOneFunction:
    def test_a_file_inside_the_workspace_passes(self, tmp_path) -> None:
        assert gate.refuse_to_send(make(tmp_path, "notes.txt"), tmp_path) is None

    def test_outside_the_workspace_is_refused(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = make(tmp_path, "elsewhere.txt")
        assert "outside the workspace" in (gate.refuse_to_send(outside, workspace) or "")

    def test_a_symlink_out_is_refused(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        target = make(tmp_path / "outside", "secret.txt", b"not yours")
        link = workspace / "shortcut"
        link.symlink_to(target)
        assert gate.refuse_to_send(link, workspace) is not None

    def test_a_secrets_path_is_refused(self, tmp_path) -> None:
        path = make(tmp_path, ".env", b"TOKEN=abc")
        assert "secrets file" in (gate.refuse_to_send(path, tmp_path) or "")

    def test_a_credential_in_the_content_is_refused(self, tmp_path) -> None:
        path = make(tmp_path, "notes.txt", b"ghp_0123456789abcdefghijklmnopqrstuvwxyz\n")
        refusal = gate.refuse_to_send(path, tmp_path)
        assert refusal is not None
        assert "ghp_" not in refusal, "the refusal must never quote the value"

    def test_empty_missing_and_oversized_each_say_which(self, tmp_path, monkeypatch) -> None:
        assert "empty" in (gate.refuse_to_send(make(tmp_path, "z.txt", b""), tmp_path) or "")
        assert "no such file" in (gate.refuse_to_send(tmp_path / "nope.txt", tmp_path) or "")
        monkeypatch.setattr(gate, "MAX_DOCUMENT_BYTES", 4, raising=True)
        assert "over the" in (
            gate.refuse_to_send(make(tmp_path, "big.bin", b"x" * 99), tmp_path) or ""
        )

    def test_a_directory_is_refused(self, tmp_path) -> None:
        (tmp_path / "folder").mkdir()
        assert "not a file" in (gate.refuse_to_send(tmp_path / "folder", tmp_path) or "")


class TestPinnedToWhatWasApproved:
    def test_the_digest_of_a_file_is_stable(self, tmp_path) -> None:
        path = make(tmp_path, "a.txt", b"hello")
        assert gate.digest_of(path) == gate.digest_of(path)

    def test_a_rewritten_file_is_refused_as_changed(self, tmp_path) -> None:
        """C1's repro, at the gate: approved bytes, then different bytes."""
        path = make(tmp_path, "report.csv", b"month,total\n2026-09,12\n")
        approved = gate.digest_of(path)
        path.write_bytes(b"AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n")
        refusal = gate.refuse_to_send(path, tmp_path, expect_digest=approved)
        assert refusal is not None
        assert "changed_since_queued" in refusal
        assert "AWS_SECRET" not in refusal

    def test_unchanged_bytes_pass_the_pin(self, tmp_path) -> None:
        path = make(tmp_path, "report.csv", b"month,total\n")
        assert gate.refuse_to_send(path, tmp_path, expect_digest=gate.digest_of(path)) is None

    def test_no_pin_means_only_the_ladder_runs(self, tmp_path) -> None:
        path = make(tmp_path, "report.csv", b"month,total\n")
        assert gate.refuse_to_send(path, tmp_path, expect_digest=None) is None


class TestResolve:
    def test_a_relative_path_resolves_against_the_workspace(self, tmp_path) -> None:
        make(tmp_path, "sub/a.txt")
        resolved, refusal = gate.resolve_for_send("sub/a.txt", tmp_path)
        assert refusal is None
        assert resolved == (tmp_path / "sub" / "a.txt").resolve()

    def test_traversal_in_a_relative_path_is_refused(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        _resolved, refusal = gate.resolve_for_send("../../../../etc/passwd", workspace)
        assert refusal is not None

    def test_an_empty_path_is_refused(self, tmp_path) -> None:
        _resolved, refusal = gate.resolve_for_send("", tmp_path)
        assert refusal is not None
