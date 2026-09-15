"""The agent-bundle envelope: its schema, its hashes, and the two export gates.

Everything here is about the bundle as a *document* — parsing it, verifying the
files it claims, and refusing content that must never leave an instance. The
export that produces one and the install that consumes one are tested next door.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from robothor.templates.bundle import (
    BUNDLE_FILENAME,
    BUNDLE_KIND,
    BUNDLE_SCHEMA,
    BundleError,
    bundle_document,
    parse_bundle,
    read_bundle,
    scan_instance_leaks,
    scan_secret_literals,
    verify_bundle_files,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_bundle(root, files: dict[str, str], **overrides) -> dict:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    document = {
        "kind": BUNDLE_KIND,
        "schema": BUNDLE_SCHEMA,
        "id": "test-agent",
        "name": "Test Agent",
        "version": "1.0.0",
        "exported_at": "2026-09-15T00:00:00+00:00",
        "platform_version": "1.89.0",
        "requires": {"plugins": [], "adapters": [], "secrets": [], "skills": []},
        "files": [
            {"path": name, "sha256": _sha256(content)} for name, content in sorted(files.items())
        ],
    }
    document.update(overrides)
    (root / BUNDLE_FILENAME).write_text(yaml.dump(document, sort_keys=False))
    return document


class TestParse:
    def test_round_trips_through_the_document_writer(self, tmp_path):
        document = _write_bundle(tmp_path, {"setup.yaml": "agent_id: test-agent\n"})
        manifest = parse_bundle(document)

        assert manifest.id == "test-agent"
        assert manifest.version == "1.0.0"
        assert [f.path for f in manifest.files] == ["setup.yaml"]
        assert parse_bundle(yaml.safe_load(bundle_document(manifest))) == manifest

    def test_refuses_a_foreign_kind(self, tmp_path):
        document = _write_bundle(tmp_path, {"setup.yaml": "x\n"}, kind="plugin")
        with pytest.raises(BundleError, match="agent-bundle"):
            parse_bundle(document)

    def test_refuses_a_future_schema(self, tmp_path):
        document = _write_bundle(tmp_path, {"setup.yaml": "x\n"}, schema=2)
        with pytest.raises(BundleError, match="schema"):
            parse_bundle(document)

    def test_refuses_a_traversing_file_path(self, tmp_path):
        document = _write_bundle(tmp_path, {"setup.yaml": "x\n"})
        document["files"] = [{"path": "../escape.yaml", "sha256": "a" * 64}]
        with pytest.raises(BundleError, match="path"):
            parse_bundle(document)

    def test_refuses_a_non_identifier_id(self, tmp_path):
        document = _write_bundle(tmp_path, {"setup.yaml": "x\n"}, id="../../etc")
        with pytest.raises(BundleError):
            parse_bundle(document)


class TestVerifyFiles:
    def test_accepts_a_bundle_whose_hashes_match(self, tmp_path):
        _write_bundle(tmp_path, {"setup.yaml": "agent_id: test-agent\n", "SKILL.md": "# s\n"})
        verify_bundle_files(tmp_path, read_bundle(tmp_path))

    def test_refuses_a_tampered_file(self, tmp_path):
        _write_bundle(tmp_path, {"setup.yaml": "agent_id: test-agent\n"})
        manifest = read_bundle(tmp_path)
        (tmp_path / "setup.yaml").write_text("agent_id: other\n")
        with pytest.raises(BundleError, match="setup.yaml"):
            verify_bundle_files(tmp_path, manifest)

    def test_refuses_a_member_the_file_list_omits(self, tmp_path):
        """The attack: ship a payload nothing in the signed list covers."""
        _write_bundle(tmp_path, {"setup.yaml": "agent_id: test-agent\n"})
        manifest = read_bundle(tmp_path)
        (tmp_path / "stowaway.md").write_text("# not listed\n")
        with pytest.raises(BundleError, match="stowaway.md"):
            verify_bundle_files(tmp_path, manifest)

    def test_refuses_a_listed_file_that_is_missing(self, tmp_path):
        _write_bundle(tmp_path, {"setup.yaml": "agent_id: test-agent\n"})
        manifest = read_bundle(tmp_path)
        (tmp_path / "setup.yaml").unlink()
        with pytest.raises(BundleError, match="setup.yaml"):
            verify_bundle_files(tmp_path, manifest)

    def test_refuses_a_symlinked_member(self, tmp_path):
        _write_bundle(tmp_path, {"setup.yaml": "agent_id: test-agent\n"})
        manifest = read_bundle(tmp_path)
        (tmp_path / "setup.yaml").unlink()
        (tmp_path / "setup.yaml").symlink_to("/etc/passwd")
        with pytest.raises(BundleError):
            verify_bundle_files(tmp_path, manifest)


class TestSecretScan:
    def test_finds_an_api_key_in_a_code_block(self):
        text = "# Agent\n\n```\nexport OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnop\n```\n"
        hits = scan_secret_literals(text, "brain/AGENT.md")

        assert [h.line for h in hits] == [4]
        assert hits[0].path == "brain/AGENT.md"
        assert "sk-or-v1-abcdefghijklmnop" not in hits[0].describe()

    def test_finds_a_named_assignment_with_no_shape_of_its_own(self):
        hits = scan_secret_literals("SMTP_PASSWORD=hunter2\n", "setup.yaml")
        assert [h.line for h in hits] == [1]

    def test_leaves_an_env_placeholder_alone(self):
        text = 'to: "${ROBOTHOR_TELEGRAM_CHAT_ID}"\nheaders:\n  Authorization: "Bearer ${TOKEN}"\n'
        assert scan_secret_literals(text, "manifest.template.yaml") == []

    def test_leaves_ordinary_prose_alone(self):
        text = "Use the key-value store. Sort key ordering matters. Ask for a token.\n"
        assert scan_secret_literals(text, "brain/AGENT.md") == []


#: Every case the hostile review exported cleanly. Assembled from pieces so the
#: test file itself carries no credential-shaped run — gitleaks and the gate
#: under test are looking at the same bytes, and a fixture that tripped either
#: would be a fixture nobody could commit.
_GH = "ghp_" + "A" * 36
_GH_PAT = "github_pat_" + "B" * 22 + "_" + "C" * 59
_GITLAB = "glpat-" + "D" * 20
_AWS = "AKIA" + "IOSFODNN7EXAMPL"
_GOOGLE = "AIza" + "E" * 35
_PEM = "-----BEGIN RSA PRIVATE" + " KEY-----\nMIIEow…\n-----END RSA PRIVATE KEY-----"
_JWT = "eyJhbGciOiJIUzI1NiJ9." + "F" * 24 + "." + "G" * 43


class TestCredentialShapes:
    """The value-shape half of the gate. Each case is a review finding."""

    @pytest.mark.parametrize(
        ("label", "line"),
        [
            ("url userinfo", f"endpoint: https://user:{_GH}@host/x"),
            ("url userinfo, plain password", "url: https://svc:S3cretP4ssw0rdLong@b.example/_mcp"),
            ("github token", f"github_pat: {_GH}"),
            ("github fine-grained", f"token: {_GH_PAT}"),
            ("gitlab token", f"Authenticate with {_GITLAB} before calling."),
            ("aws access key", f"access_key_id: {_AWS}"),
            ("google api key", f"Call the maps API with key {_GOOGLE}."),
            ("pem private key", _PEM),
            ("jwt", f"session: {_JWT}"),
            ("inside a command", f'command: ["curl", "-H", "X-Key: {_GH}", "https://x"]'),
        ],
    )
    def test_the_shape_is_refused_wherever_it_appears(self, label, line):
        hits = scan_secret_literals(line + "\n", "manifest.template.yaml")
        assert hits, f"{label} exported cleanly"
        assert all(part not in hits[0].describe() for part in (_GH, _AWS, _GOOGLE, _GITLAB))

    def test_a_credential_word_inside_a_longer_key_still_counts(self):
        """``access_key_id:`` missed: the word had to TERMINATE the key name."""
        assert scan_secret_literals("access_key_id: AKIAsomethingelse\n", "adapters/b.yaml")

    def test_a_password_stated_in_prose_is_refused(self):
        hits = scan_secret_literals("# the billing password is hunter2hunter2\n", "setup.yaml")
        assert [h.line for h in hits] == [1]

    @pytest.mark.parametrize(
        "line",
        [
            "Read the API docs before you change the password handling.",
            "The token is described below; the secret is stored in the vault.",
            "url: https://billing.example.com/_mcp",
            "repo: ssh://alice@example.com/acme/agents",
            "protocol: 2026-07-28",
            "status_file: brain/memory/agent-status.md",
            "Sort key ordering matters, and the partition key is not a secret.",
            "  - GET /api/conversations",
            "model:\n  primary: openrouter/xiaomi/mimo-v2-pro",
            "token_path: /run/secrets/agent",
            "max_tokens: 4096",
        ],
    )
    def test_ordinary_platform_text_is_left_alone(self, line):
        assert scan_secret_literals(line + "\n", "manifest.template.yaml") == []


class TestTheShippedCorpus:
    """Nothing legitimate is blocked — measured, not asserted.

    The hostile review ran both gates over every tracked template artefact and
    found zero hits; widening the gate is only safe while that stays true, so
    the measurement lives here rather than in a scratch script that ran once.
    """

    def test_no_shipped_template_artefact_trips_either_gate(self):
        root = Path(__file__).resolve().parents[3] / "templates"
        members = [
            path
            for pattern in ("*.yaml", "*.yml", "*.md", "*.json")
            for path in root.rglob(pattern)
            if path.is_file()
        ]
        assert len(members) > 50, f"corpus collapsed to {len(members)} files"

        findings = []
        for path in members:
            text = path.read_text(encoding="utf-8", errors="replace")
            relative = path.relative_to(root).as_posix()
            findings.extend(scan_secret_literals(text, relative))
            findings.extend(scan_instance_leaks(text, relative))
        assert not findings, [f.describe() for f in findings]


#: The fixtures below are the exact shape the leak gate refuses, so they are
#: assembled at runtime rather than written out: a test file that spelled one
#: literally would be caught by ``scripts/check_instance_leak.py`` — which is
#: the same rule, doing its job on the file testing it.
_LINUX_HOME = "/home/" + "alice"
_MAC_HOME = "/Users/" + "bob"


class TestLeakScan:
    def test_finds_a_home_path(self):
        hits = scan_instance_leaks(f"Read {_LINUX_HOME}/robothor/brain/notes.md first.\n", "S.md")
        assert [h.line for h in hits] == [1]

    def test_finds_a_mac_home_path(self):
        hits = scan_instance_leaks(f"cd {_MAC_HOME}/robothor\n", "SKILL.md")
        assert [h.line for h in hits] == [1]

    def test_leaves_a_workspace_relative_path_alone(self):
        text = "status_file: brain/memory/test-agent-status.md\nGET /api/health\n"
        assert scan_instance_leaks(text, "manifest.template.yaml") == []
