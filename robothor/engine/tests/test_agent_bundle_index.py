"""A signed index can publish agent bundles as well as plugin wheels.

One index, one signature, one set of pinned keys — and two kinds of thing in
it. The properties that matter are all about the two never being confused:

* an entry says which kind it is, and its artifacts must match that kind
* ``genus agent install`` will not take a plugin entry, and ``genus plugin
  install`` will not take an agent bundle — each refusal names the verb that
  DOES take it, because "not found" would send the operator hunting
* everything the plugin path already refuses (a tampered document, an unpinned
  key, a stale index) is refused identically for a bundle entry, because it is
  the same signature over the same canonical bytes
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from robothor.plugins import registry

KEY_ID = "test-key-1"

BUNDLE_ENTRY = {
    "kind": "agent-bundle",
    "name": "triage-bot",
    "version": "1.2.0",
    "summary": "Triages inbound mail",
    "requires": {
        "plugins": ["genus-billing"],
        "adapters": [],
        "secrets": ["BILLING_API_KEY"],
        "skills": ["triage"],
    },
    "artifacts": [
        {
            "kind": "bundle",
            "filename": "agent-triage-bot-1.2.0.tar.gz",
            "url": "https://example.invalid/agent-triage-bot-1.2.0.tar.gz",
            "sha256": "b" * 64,
            "size": 4096,
        }
    ],
}

PLUGIN_ENTRY = {
    "name": "genus-hostinfo",
    "version": "0.1.0",
    "manifest_sha256": "d" * 64,
    "artifacts": [
        {
            "kind": "wheel",
            "filename": "genus_hostinfo-0.1.0-py3-none-any.whl",
            "url": "https://example.invalid/genus_hostinfo-0.1.0-py3-none-any.whl",
            "sha256": "a" * 64,
            "size": 2048,
        }
    ],
}


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _signed(entries: list[dict]) -> tuple[bytes, bytes, dict[str, str]]:
    private_pem, public_pem = _keypair()
    payload = {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "publisher": {"id": "genus", "key_id": KEY_ID},
        "plugins": entries,
    }
    raw = registry.canonical_bytes(payload)
    signature = registry.signature_document(payload, private_pem, key_id=KEY_ID).encode()
    return raw, signature, {KEY_ID: public_pem}


def _index(entries: list[dict]) -> registry.Index:
    raw, signature, keys = _signed(entries)
    return registry.parse_index(raw, signature, keys=keys, url="https://example.invalid/index.json")


class TestParsing:
    def test_a_bundle_entry_parses_with_its_requirements(self):
        index = _index([BUNDLE_ENTRY])
        entry = index.entry("triage-bot", kind="agent-bundle")

        assert entry is not None
        assert entry.kind == "agent-bundle"
        assert entry.requires.plugins == ("genus-billing",)
        assert entry.requires.secrets == ("BILLING_API_KEY",)
        assert entry.bundle() is not None
        assert entry.wheel() is None

    def test_a_plugin_entry_still_parses_without_declaring_a_kind(self):
        index = _index([PLUGIN_ENTRY])
        entry = index.entry("genus-hostinfo", kind="plugin")

        assert entry is not None
        assert entry.kind == "plugin"
        assert entry.wheel() is not None
        assert entry.bundle() is None

    def test_a_bundle_entry_carrying_a_wheel_is_refused(self):
        bad = {**BUNDLE_ENTRY, "artifacts": PLUGIN_ENTRY["artifacts"]}
        with pytest.raises(registry.RegistryError, match="bundle"):
            _index([bad])

    def test_a_plugin_entry_carrying_a_bundle_is_refused(self):
        bad = {**PLUGIN_ENTRY, "artifacts": BUNDLE_ENTRY["artifacts"]}
        with pytest.raises(registry.RegistryError, match="wheel"):
            _index([bad])

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(registry.RegistryError, match="kind"):
            _index([{**BUNDLE_ENTRY, "kind": "workflow"}])

    def test_a_tampered_bundle_entry_fails_the_signature(self):
        raw, signature, keys = _signed([BUNDLE_ENTRY])
        tampered = raw.replace(b'"sha256":"' + b"b" * 64, b'"sha256":"' + b"c" * 64)
        assert tampered != raw
        with pytest.raises(registry.RegistryError, match="signature"):
            registry.parse_index(tampered, signature, keys=keys)


class TestSelect:
    def test_selecting_a_bundle_returns_the_bundle_entry(self):
        index = _index([PLUGIN_ENTRY, BUNDLE_ENTRY])
        entry, source = registry.select("triage-bot", indexes=[index], kind="agent-bundle")
        assert entry.name == "triage-bot"
        assert source.key_id == KEY_ID

    def test_a_plugin_name_asked_for_as_a_bundle_names_the_right_verb(self):
        index = _index([PLUGIN_ENTRY])
        with pytest.raises(registry.RegistryError, match="genus plugin install"):
            registry.select("genus-hostinfo", indexes=[index], kind="agent-bundle")

    def test_a_bundle_name_asked_for_as_a_plugin_names_the_right_verb(self):
        index = _index([BUNDLE_ENTRY])
        with pytest.raises(registry.RegistryError, match="genus agent install"):
            registry.select("triage-bot", indexes=[index])

    def test_a_yanked_bundle_is_refused(self):
        index = _index([{**BUNDLE_ENTRY, "yanked": True, "yank_reason": "leaked a key"}])
        with pytest.raises(registry.RegistryError, match="yanked"):
            registry.select("triage-bot", indexes=[index], kind="agent-bundle")

    def test_a_missing_name_still_says_where_it_looked(self):
        index = _index([BUNDLE_ENTRY])
        with pytest.raises(registry.RegistryError, match="example.invalid"):
            registry.select("nobody", indexes=[index], kind="agent-bundle")
