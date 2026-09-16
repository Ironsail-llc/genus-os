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

import pytest

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


class TestTheScanIsNotExtensionGated:
    """Hostile review I2. The scan ran only for a suffix on a list, so renaming
    was the whole attack: `tok.txt` was refused and the same bytes as `tok.png`
    were sent. The `UnicodeDecodeError` catch is the "is this actually text?"
    test and it is strictly better than a suffix.
    """

    GITHUB = b"GITHUB_TOKEN: ghp_0123456789abcdefghijklmnopqrstuvwxyz\n"
    AWS = b"aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
    PEM = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----\n"

    @pytest.mark.parametrize(
        "name",
        [
            "tok.txt",
            "tok.md",
            "tok.csv",
            "tok.yaml",
            "notes",
            "token.bin",
            "tok.png",
            "tok.dat",
            "tok.jpeg",
            "tok.zip",
            "tok.exe",
            "tok",
        ],
    )
    def test_a_github_token_is_refused_whatever_the_file_is_called(self, tmp_path, name) -> None:
        assert gate.refuse_to_send(make(tmp_path, name, self.GITHUB), tmp_path) is not None

    @pytest.mark.parametrize("name", ["aws.bin", "aws.png", "aws.txt"])
    def test_an_aws_key_id_is_refused_whatever_the_file_is_called(self, tmp_path, name) -> None:
        assert gate.refuse_to_send(make(tmp_path, name, self.AWS), tmp_path) is not None

    @pytest.mark.parametrize("name", ["key.bin", "key.png", "key.pem.txt"])
    def test_a_pem_private_key_is_refused_whatever_the_file_is_called(self, tmp_path, name) -> None:
        assert gate.refuse_to_send(make(tmp_path, name, self.PEM), tmp_path) is not None

    def test_a_real_binary_is_not_refused_by_the_scan(self, tmp_path) -> None:
        """The decode is the binary test; a PNG must still be sendable.

        The fixture is deliberately **over 4 MB**. The first version of this
        test used a 2 KB PNG and passed while `send_file` was refusing every
        binary above `MAX_SCAN_BYTES` — a test that could not see the
        regression it was supposed to cover (re-review R1).
        """
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 48_000  # ~12 MB
        assert len(png) > 4 * 1024 * 1024
        assert gate.refuse_to_send(make(tmp_path, "chart.png", png), tmp_path) is None

    def test_a_six_megabyte_pdf_is_not_refused_by_the_scan(self, tmp_path) -> None:
        """The brief's own case: "send me that as a PDF" for a real report."""
        pdf = b"%PDF-1.7\n" + bytes(range(256)) * 24_000  # ~6 MB
        assert len(pdf) > 4 * 1024 * 1024
        assert gate.refuse_to_send(make(tmp_path, "report.pdf", pdf), tmp_path) is None

    def test_a_twenty_megabyte_video_is_not_refused_by_the_scan(self, tmp_path) -> None:
        clip = b"\x00\x00\x00 ftypmp42" + bytes(range(256)) * 80_000  # ~20 MB
        assert gate.refuse_to_send(make(tmp_path, "clip.mp4", clip), tmp_path) is None

    def test_a_large_clean_text_file_is_not_refused(self, tmp_path) -> None:
        """Over MAX_SCAN_BYTES and still text — only the head is read, so the
        size of the file has nothing to do with whether it can be checked."""
        body = b"month,total\n2026-09,12\n" * 250_000  # ~5.5 MB
        assert len(body) > 4 * 1024 * 1024
        assert gate.refuse_to_send(make(tmp_path, "report.csv", body), tmp_path) is None

    def test_a_credential_in_a_large_text_file_is_still_caught(self, tmp_path) -> None:
        """Dropping the size rung must not drop the scan with it."""
        body = b"ghp_0123456789abcdefghijklmnopqrstuvwxyz\n" + b"x,y\n" * 2_000_000
        assert len(body) > 4 * 1024 * 1024
        assert gate.refuse_to_send(make(tmp_path, "rows.csv", body), tmp_path) is not None

    def test_a_multibyte_character_split_by_the_head_read_is_still_text(
        self, tmp_path, monkeypatch
    ) -> None:
        """A truncating read can cut a UTF-8 sequence in half. Treating that as
        "binary" would silently stop scanning a real text file — the exact
        class of bug this finding is about."""
        token = b"ghp_0123456789abcdefghijklmnopqrstuvwxyz\n"  # 41 bytes
        body = token + b"xx" + "€ and more text".encode()
        # Stop the read one byte into the three-byte `€`.
        monkeypatch.setattr(gate, "SCAN_HEAD_BYTES", len(token) + 3, raising=True)
        assert gate.refuse_to_send(make(tmp_path, "notes.dat", body), tmp_path) is not None

    def test_a_token_past_the_head_is_not_scanned_and_that_is_stated(
        self, tmp_path, monkeypatch
    ) -> None:
        """An honest limit: only the head is read, so a credential buried past
        it is not seen. Pinned so the boundary is a decision, not a surprise."""
        monkeypatch.setattr(gate, "SCAN_HEAD_BYTES", 64, raising=True)
        body = b"x" * 200 + b"ghp_0123456789abcdefghijklmnopqrstuvwxyz\n"
        assert gate.refuse_to_send(make(tmp_path, "notes.txt", body), tmp_path) is None


class TestCredentialKeyNames:
    """A credentials file has no token-shaped literal in it.

    Re-review, the exec-exposure section. `cp <secret>/BQAD-77-credentials.json
    c.json` moves the bytes out of the `secret/` directory — and the directory
    IS the flag — so the content scan becomes the only thing left. A Google
    OAuth client secret is just a short opaque string, which
    `scan_secret_literals` has no pattern for, and the file went out in full.

    The other half of that exposure (teaching `secret_paths` the inbox layout so
    `exec` cannot `cat` the original) is deliberately NOT here: that module
    belongs to `feat/vault-managed-secrets` until #576 merges.
    """

    def test_the_reviewers_client_secret_is_refused(self, tmp_path) -> None:
        path = make(tmp_path, "c.json", b'{"client_secret":"abc123"}')
        refusal = gate.refuse_to_send(path, tmp_path)
        assert refusal is not None
        assert "abc123" not in refusal, "the refusal must never quote the value"

    @pytest.mark.parametrize(
        "key",
        [
            "client_secret",
            "private_key",
            "api_key",
            "apiKey",
            "access_token",
            "refresh_token",
            "password",
            "secret",
            "token",
            "CLIENT_SECRET",
        ],
    )
    def test_each_credential_key_name_is_refused_in_json(self, tmp_path, key) -> None:
        path = make(tmp_path, "c.json", f'{{"{key}": "s3cr3tliteral"}}'.encode())
        assert gate.refuse_to_send(path, tmp_path) is not None, key

    @pytest.mark.parametrize(
        "key", ["client_secret", "private_key", "api_key", "access_token", "password"]
    )
    def test_each_credential_key_name_is_refused_in_yaml(self, tmp_path, key) -> None:
        path = make(tmp_path, "c.yaml", f"{key}: s3cr3tliteral\n".encode())
        assert gate.refuse_to_send(path, tmp_path) is not None, key

    def test_a_nested_google_oauth_file_is_refused(self, tmp_path) -> None:
        body = (
            b'{"installed": {"client_id": "1234.apps.googleusercontent.com",\n'
            b'  "project_id": "my-project",\n'
            b'  "client_secret": "GOCSPX-abcdefghijklmnop"}}\n'
        )
        assert gate.refuse_to_send(make(tmp_path, "c.json", body), tmp_path) is not None

    def test_an_empty_value_is_not_a_credential(self, tmp_path) -> None:
        """A template or an example is not a secret — refusing it would make
        every scaffold unsendable, which is how a gate gets turned off."""
        for body in (b'{"client_secret": ""}', b"password:\n", b'{"token": null}'):
            path = make(tmp_path, "tpl.json", body)
            assert gate.refuse_to_send(path, tmp_path) is None, body

    def test_a_placeholder_reference_is_not_a_credential(self, tmp_path) -> None:
        path = make(tmp_path, "cfg.yaml", b"api_key: ${BILLING_API_KEY}\n")
        assert gate.refuse_to_send(path, tmp_path) is None

    def test_an_ordinary_word_ending_in_token_is_not_a_credential(self, tmp_path) -> None:
        """`max_tokens` and `token_path` are a count and a filename;
        `bundle.py` already argues this and the key rule must agree with it.

        (`tokenizer: gpt2` is deliberately not here: the SHARED scanner already
        refuses it, which is a pre-existing conservative call in
        `templates/bundle.py` and not this module's to relax.)
        """
        for body in (
            b'{"max_tokens": 4096}',
            b'{"token_path": "/etc/app/token"}',
            b"max_tokens: 4096\n",
        ):
            path = make(tmp_path, "cfg.json", body)
            assert gate.refuse_to_send(path, tmp_path) is None, body

    def test_a_disabled_setting_is_not_a_credential(self, tmp_path) -> None:
        for body in (b'{"token": null}', b"password: false\n", b"api_key: ~\n"):
            path = make(tmp_path, "cfg.json", body)
            assert gate.refuse_to_send(path, tmp_path) is None, body

    def test_a_csv_column_called_password_is_not_a_credential(self, tmp_path) -> None:
        """The rule is `key: value`, not the word appearing anywhere."""
        body = b"name,password_changed_at\nAlice,2026-01-02\nBob,2026-03-04\n"
        assert gate.refuse_to_send(make(tmp_path, "rows.csv", body), tmp_path) is None


class TestCredentialExclusionsThroughTheRealGate:
    """Re-review R7. The exclusions existed and were tested — on
    ``_credential_key_line``, in isolation. They never ran end to end, because
    ``_credential_refusal`` only reaches that helper when
    ``scan_secret_literals`` found nothing, and the shared scanner has its own
    "credential-named field" rule with no exclusions at all. So `send_file`
    refused ordinary config scaffolds, a README, and a commented-out example.

    **Every case here goes through `refuse_to_send`.** Asserting on the helper
    is what let this through, and a test that cannot see the defect it is named
    for is worse than no test.
    """

    SENDS = [
        pytest.param("cfg.yaml", b"password: ${PASSWORD}\n", id="yaml-env-ref"),
        pytest.param("cfg.json", b'{"client_secret": "${CLIENT_SECRET}"}\n', id="json-env-ref"),
        pytest.param("cfg.toml", b'password = "${DB_PASSWORD}"\n', id="toml-env-ref"),
        pytest.param("cfg.yaml", b'password: ""\n', id="yaml-empty"),
        pytest.param("cfg.json", b'{"password": ""}\n', id="json-empty"),
        pytest.param("cfg.yaml", b"api_key: null\n", id="null"),
        pytest.param("cfg.yaml", b"password: none\n", id="none"),
        pytest.param("cfg.yaml", b"secret: false\n", id="false"),
        pytest.param("cfg.yaml", b"token: ~\n", id="tilde"),
        pytest.param("cfg.yaml", b"max_tokens: 4096\n", id="bare-number"),
        pytest.param("cfg.yaml", b"token: 12345\n", id="numeric-token"),
        pytest.param("cfg.yaml", b"token_path: /etc/app/token\n", id="token-path"),
        pytest.param("rows.csv", b"id,password_changed_at\n1,2026-01-02\n", id="csv-column"),
        pytest.param("cfg.yaml", b"token: disabled\n", id="disabled"),
        pytest.param("cfg.yaml", b"secret: REDACTED\n", id="redacted"),
        pytest.param("cfg.yaml", b"api_key: YOUR_API_KEY_HERE\n", id="your-key-here"),
        pytest.param("cfg.yaml", b"password: changeme\n", id="changeme"),
        pytest.param("cfg.yaml", b"secret: !vault |\n  encrypted\n", id="yaml-vault-tag"),
        pytest.param(
            "readme.md",
            b"Set the token: paste it into the field and save.\n",
            id="prose",
        ),
        pytest.param("example.yaml", b"# password: hunter2\n", id="commented-out"),
        pytest.param("example.yaml", b"  # api_key: abcdef\n", id="indented-comment"),
    ]

    @pytest.mark.parametrize(("name", "body"), SENDS)
    def test_a_scaffold_or_a_document_still_sends(self, tmp_path, name, body) -> None:
        refusal = gate.refuse_to_send(make(tmp_path, name, body), tmp_path)
        assert refusal is None, refusal

    REFUSES = [
        pytest.param("cfg.yaml", b"password: hunter2\n", id="yaml-real"),
        pytest.param("cfg.json", b'{"client_secret":"abc123"}\n', id="json-real"),
        pytest.param("key.json", b'  "private_key": "-----BEGIN RSA PRIVATE KEY-----"\n', id="pem"),
        pytest.param("cfg.yaml", b"password: hunter2  # the live one\n", id="trailing-comment"),
        pytest.param(
            "cfg.json",
            b'{"env": "prod", "client_secret": "GOCSPX-abcdefghij"}\n',
            id="second-key-on-the-line",
        ),
        pytest.param("notes.txt", b"ghp_0123456789abcdefghijklmnopqrstuvwxyz\n", id="bare-token"),
    ]

    @pytest.mark.parametrize(("name", "body"), REFUSES)
    def test_a_real_credential_is_still_refused(self, tmp_path, name, body) -> None:
        refusal = gate.refuse_to_send(make(tmp_path, name, body), tmp_path)
        assert refusal is not None
        for secret in (b"hunter2", b"abc123", b"GOCSPX", b"ghp_0123"):
            assert secret.decode() not in refusal, "the refusal must never quote the value"

    def test_a_scaffold_with_one_real_secret_in_it_is_refused(self, tmp_path) -> None:
        """Excluding the placeholder lines must not excuse the file."""
        body = b"api_key: ${API_KEY}\npassword: \nlive_token: ghp_0123456789abcdefghijk\n"
        assert gate.refuse_to_send(make(tmp_path, "cfg.yaml", body), tmp_path) is not None

    def test_a_dash_run_is_not_a_comment(self, tmp_path) -> None:
        """Regression, found building this: `--` was in the comment markers for
        SQL, and PEM armour starts `-----BEGIN`. That made a private key look
        like a comment — a false NEGATIVE, the opposite of the mistake these
        exclusions guard against."""
        pem = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----\n"
        assert gate.refuse_to_send(make(tmp_path, "k.txt", pem), tmp_path) is not None

    def test_a_lowercase_value_ending_in_here_is_not_a_placeholder(self, tmp_path) -> None:
        """Also found building this. `.*[_-]here` matched `opaque-value-here`,
        so a real value would have been excused for ending in an English word.
        A placeholder SHOUTS: the rule is case-sensitive now."""
        body = b"client_secret: some-value-here\n"
        assert gate.refuse_to_send(make(tmp_path, "cfg.yaml", body), tmp_path) is not None
        shouted = b"client_secret: YOUR_SECRET_HERE\n"
        assert gate.refuse_to_send(make(tmp_path, "tpl.yaml", shouted), tmp_path) is None


class TestHardLinks:
    """Hostile review I3. Resolving symlinks before judging containment is
    right, and hard links are immune to it: a hard link is a second NAME for the
    same inode, and there is nothing in the path for the resolver to follow.

    On the review box the workspace and ``~/.ssh`` share a filesystem, so
    ``ln ~/.ssh/id_rsa ~/robothor/notes.bin`` was a working exfiltration of the
    operator's private key — and a ``.bin`` suffix skipped the scan as well.
    """

    def test_a_hardlink_to_a_file_outside_the_workspace_is_refused(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = make(tmp_path, "outside_secret.bin", b"\x00\x01pretend this is a key")
        link = workspace / "hard.bin"
        link.hardlink_to(outside)
        refusal = gate.refuse_to_send(link, workspace)
        assert refusal is not None
        assert "more than one name" in refusal

    def test_the_refusal_says_what_to_do_instead(self, tmp_path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = make(tmp_path, "o.bin", b"\x00\x01x")
        link = workspace / "hard.bin"
        link.hardlink_to(outside)
        assert "Copy it" in (gate.refuse_to_send(link, workspace) or "")

    def test_a_hardlink_that_stays_inside_is_refused_too(self, tmp_path) -> None:
        """Blunt on purpose. Deciding that every OTHER name for an inode is
        also inside the workspace means walking the whole tree on every send;
        a legitimately hard-linked file can be copied instead."""
        original = make(tmp_path, "a.txt", b"hello")
        link = tmp_path / "b.txt"
        link.hardlink_to(original)
        assert gate.refuse_to_send(link, tmp_path) is not None

    def test_an_ordinary_single_linked_file_still_passes(self, tmp_path) -> None:
        assert gate.refuse_to_send(make(tmp_path, "a.txt", b"hello"), tmp_path) is None

    def test_a_copy_of_the_hardlinked_file_passes(self, tmp_path) -> None:
        """The remedy the refusal names actually works."""
        original = make(tmp_path, "a.txt", b"hello")
        link = tmp_path / "b.txt"
        link.hardlink_to(original)
        copy = tmp_path / "c.txt"
        copy.write_bytes(link.read_bytes())
        assert gate.refuse_to_send(copy, tmp_path) is None


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
