"""The index builder publishes agent bundles alongside wheels.

The kind is read out of the artifact — ``bundle.yaml`` says what the tarball is
and what it requires — for the same reason every plugin field is read out of the
wheel: a publisher typing those by hand inside a signed document would be
marking their own homework, and no installer downstream could notice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from robothor.plugins import registry
from robothor.templates.exporter import export_agent

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "build_plugin_index.py"

MANIFEST = """\
id: triage-bot
name: Triage Bot
description: Triages inbound mail
version: "1.2.0"
department: custom

model:
  primary: openrouter/xiaomi/mimo-v2-pro

schedule:
  cron: "0 * * * *"
  timezone: UTC

delivery:
  mode: none

tools_allowed: []
instruction_file: brain/agents/triage-bot.md

requires:
  plugins: [genus-billing]
  secrets: [BILLING_API_KEY]
"""


def _signing_key(tmp_path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "signing.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


@pytest.fixture
def exported(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "docs" / "agents").mkdir(parents=True)
    (workspace / "brain" / "agents").mkdir(parents=True)
    (workspace / "templates" / "agents").mkdir(parents=True)
    (workspace / "templates" / "agents" / "_defaults.yaml").write_text(yaml.dump({}))
    (workspace / "docs" / "agents" / "triage-bot.yaml").write_text(MANIFEST)
    (workspace / "brain" / "agents" / "triage-bot.md").write_text("# Triage Bot\n")

    dist = tmp_path / "dist"
    dist.mkdir()
    export_agent("triage-bot", out=dist / "agent-triage-bot-1.2.0.tar.gz", repo_root=workspace)
    return dist


def _build(dist: Path, tmp_path: Path) -> tuple[int, str, Path]:
    key = _signing_key(tmp_path)
    out = tmp_path / "index.json"
    completed = subprocess.run(  # noqa: S603 - the script under test, with fixture paths
        [
            sys.executable,
            str(SCRIPT),
            str(dist),
            "--out",
            str(out),
            "--key",
            str(key),
            "--key-id",
            "test-key-1",
            "--base-url",
            "https://example.invalid/artifacts/",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr, out


def test_a_bundle_tarball_becomes_a_signed_agent_bundle_entry(exported, tmp_path):
    code, output, out = _build(exported, tmp_path)
    assert code == 0, output

    payload = json.loads(out.read_bytes())
    entries = {e["name"]: e for e in payload["plugins"]}
    entry = entries["triage-bot"]

    assert entry["kind"] == "agent-bundle"
    assert entry["version"] == "1.2.0"
    assert entry["requires"]["plugins"] == ["genus-billing"]
    assert entry["artifacts"][0]["kind"] == "bundle"
    assert entry["artifacts"][0]["url"].endswith("agent-triage-bot-1.2.0.tar.gz")


def test_the_index_it_writes_verifies_with_the_shipped_parser(exported, tmp_path):
    code, output, out = _build(exported, tmp_path)
    assert code == 0, output

    # The builder self-verifies before announcing; prove the entry is reachable
    # through the same select() an installer would use.
    raw = out.read_bytes()
    signature = Path(str(out) + ".sig").read_bytes()
    key_id = json.loads(signature)["key_id"]
    private = serialization.load_pem_private_key(
        (tmp_path / "signing.pem").read_bytes(), password=None
    )
    public = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    index = registry.parse_index(raw, signature, keys={key_id: public})
    entry, _ = registry.select("triage-bot", indexes=[index], kind="agent-bundle")
    assert entry.bundle() is not None


def test_a_directory_with_neither_wheels_nor_bundles_is_refused(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, output, _ = _build(empty, tmp_path)
    assert code == 2
    assert "refusing to sign an empty index" in output
