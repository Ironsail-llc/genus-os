"""The agent-bundle envelope: its schema, its hashes, and the two export gates.

Everything here is about the bundle as a *document* — parsing it, verifying the
files it claims, and refusing content that must never leave an instance. The
export that produces one and the install that consumes one are tested next door.
"""

from __future__ import annotations

import hashlib

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


class TestLeakScan:
    def test_finds_a_home_path(self):
        hits = scan_instance_leaks("Read /home/alice/robothor/brain/notes.md first.\n", "SKILL.md")
        assert [h.line for h in hits] == [1]

    def test_finds_a_mac_home_path(self):
        hits = scan_instance_leaks("cd /Users/bob/robothor\n", "SKILL.md")
        assert [h.line for h in hits] == [1]

    def test_leaves_a_workspace_relative_path_alone(self):
        text = "status_file: brain/memory/test-agent-status.md\nGET /api/health\n"
        assert scan_instance_leaks(text, "manifest.template.yaml") == []
