"""The signed index is the only thing that says a plugin is the one published.

There is no package server here and there will not be one: the registry is a
static ``index.json`` plus a detached Ed25519 signature, served from anywhere
that can serve a file. Everything that makes that safe is a refusal, and a
refusal that is not tested is a refusal that does not exist -- this platform has
shipped six controls that were built, wired, tested and completely inert.

So every property below is an attack, spelled out:

* **One byte changed anywhere in the index** must fail verification. The
  signature covers the canonical form of the whole document, and a served file
  that is not already canonical is refused rather than re-canonicalised into
  something that verifies.
* **An old index replayed** must fail. A publisher yanks a compromised plugin
  by publishing a new index; a mirror that keeps serving the previous one would
  undo that silently, so an index older than 90 days is refused on age alone.
* **A key id nobody pinned** must fail. Verification against a key that arrived
  with the document is not verification.
* **A redirect off the origin** must fail. Following one is how a pinned URL
  becomes an attacker's URL without the operator's pin ever changing.
* **Two indexes offering the same name under different publishers** must fail
  until the operator names which one they meant.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from robothor.plugins import registry

KEY_ID = "test-key-1"


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


def _payload(*, generated_at: str | None = None, plugins: list[dict] | None = None) -> dict:
    return {
        "schema": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "publisher": {"id": "genus", "key_id": KEY_ID},
        "plugins": plugins
        if plugins is not None
        else [
            {
                "name": "genus-hostinfo",
                "version": "0.1.0",
                "summary": "Host state as a tool",
                "contract_version": "1.0",
                "groups": ["genus.tools"],
                "python": ">=3.11",
                "artifacts": [
                    {
                        "kind": "wheel",
                        "filename": "genus_hostinfo-0.1.0-py3-none-any.whl",
                        "url": "https://example.invalid/genus_hostinfo-0.1.0-py3-none-any.whl",
                        "sha256": "a" * 64,
                        "size": 4096,
                    }
                ],
                "manifest_sha256": "b" * 64,
                "scan": {
                    "verdict": "safe",
                    "reasons": [],
                    "scanned_at": "2026-09-15T00:00:00+00:00",
                },
            }
        ],
    }


def _signed(payload: dict, private_pem: str, *, key_id: str = KEY_ID) -> tuple[bytes, bytes]:
    raw = registry.canonical_bytes(payload)
    sig = registry.signature_document(payload, private_pem, key_id=key_id)
    return raw, sig.encode()


# --------------------------------------------------------------------------
# canonical form
# --------------------------------------------------------------------------


def test_canonical_bytes_are_sorted_and_whitespace_free() -> None:
    out = registry.canonical_bytes({"b": 1, "a": [2, {"d": 4, "c": 3}]})
    assert out == b'{"a":[2,{"c":3,"d":4}],"b":1}'


def test_a_served_index_that_is_not_canonical_is_refused() -> None:
    """Signing a canonical form and verifying a re-canonicalised download would
    accept any change the JSON parser normalises away. The file must already be
    the bytes that were signed."""
    private_pem, public_pem = _keypair()
    payload = _payload()
    _, sig = _signed(payload, private_pem)
    pretty = json.dumps(payload, indent=2).encode()
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(pretty, sig, keys={KEY_ID: public_pem})
    assert "canonical" in str(excinfo.value)


def test_a_trailing_newline_is_tolerated() -> None:
    private_pem, public_pem = _keypair()
    raw, sig = _signed(_payload(), private_pem)
    index = registry.parse_index(raw + b"\n", sig, keys={KEY_ID: public_pem})
    assert index.entries[0].name == "genus-hostinfo"


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------


def test_a_valid_signature_parses_the_whole_entry() -> None:
    private_pem, public_pem = _keypair()
    raw, sig = _signed(_payload(), private_pem)
    index = registry.parse_index(
        raw, sig, keys={KEY_ID: public_pem}, url="https://example.invalid/index.json"
    )
    entry = index.entries[0]
    assert index.publisher_id == "genus"
    assert index.key_id == KEY_ID
    assert entry.version == "0.1.0"
    assert entry.groups == ("genus.tools",)
    assert entry.artifacts[0].sha256 == "a" * 64
    assert entry.scan.verdict == "safe"
    assert entry.yanked is False


def test_one_tampered_byte_fails_verification() -> None:
    private_pem, public_pem = _keypair()
    payload = _payload()
    raw, sig = _signed(payload, private_pem)
    tampered = raw.replace(b'"0.1.0"', b'"0.1.1"', 1)
    assert tampered != raw
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(tampered, sig, keys={KEY_ID: public_pem})
    assert "signature" in str(excinfo.value)


def test_a_tampered_sha256_fails_verification() -> None:
    """The hash is the only thing standing between the index and a swapped
    wheel, so it is inside the signed document rather than beside it."""
    private_pem, public_pem = _keypair()
    payload = _payload()
    raw, sig = _signed(payload, private_pem)
    tampered = raw.replace(b"a" * 64, b"c" * 64, 1)
    with pytest.raises(registry.RegistryError):
        registry.parse_index(tampered, sig, keys={KEY_ID: public_pem})


def test_an_unknown_key_id_is_refused() -> None:
    private_pem, public_pem = _keypair()
    raw, sig = _signed(_payload(), private_pem)
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig, keys={"other-key": public_pem})
    assert "test-key-1" in str(excinfo.value)
    assert "not pinned" in str(excinfo.value)


def test_a_signature_key_id_that_disagrees_with_the_document_is_refused() -> None:
    """Otherwise a second pinned publisher's key id can be pasted onto a
    document that names a different publisher, and the operator's own record of
    who published what becomes a lie the signature endorses."""
    private_pem, public_pem = _keypair()
    payload = _payload()
    raw = registry.canonical_bytes(payload)
    sig = registry.signature_document(payload, private_pem, key_id="someone-else")
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig.encode(), keys={"someone-else": public_pem})
    assert "key_id" in str(excinfo.value)


def test_a_missing_signature_is_refused_not_ignored() -> None:
    _, public_pem = _keypair()
    raw = registry.canonical_bytes(_payload())
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, b"", keys={KEY_ID: public_pem})
    assert "signature" in str(excinfo.value)


def test_no_pinned_keys_at_all_is_a_sentence_not_a_traceback() -> None:
    private_pem, _ = _keypair()
    raw, sig = _signed(_payload(), private_pem)
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig, keys={})
    assert "ROBOTHOR_PLUGIN_INDEX_KEYS" in str(excinfo.value)


# --------------------------------------------------------------------------
# schema and freshness
# --------------------------------------------------------------------------


def test_a_schema_other_than_one_is_refused() -> None:
    private_pem, public_pem = _keypair()
    payload = _payload()
    payload["schema"] = 2
    raw, sig = _signed(payload, private_pem)
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig, keys={KEY_ID: public_pem})
    assert "schema" in str(excinfo.value)


def test_an_index_older_than_ninety_days_is_refused() -> None:
    private_pem, public_pem = _keypair()
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat(timespec="seconds")
    raw, sig = _signed(_payload(generated_at=old), private_pem)
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig, keys={KEY_ID: public_pem})
    assert "91 days old" in str(excinfo.value)


def test_an_index_from_the_future_is_refused() -> None:
    private_pem, public_pem = _keypair()
    ahead = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
    raw, sig = _signed(_payload(generated_at=ahead), private_pem)
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig, keys={KEY_ID: public_pem})
    assert "future" in str(excinfo.value)


def test_an_unparseable_generated_at_is_refused() -> None:
    private_pem, public_pem = _keypair()
    raw, sig = _signed(_payload(generated_at="yesterday"), private_pem)
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.parse_index(raw, sig, keys={KEY_ID: public_pem})
    assert "generated_at" in str(excinfo.value)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_reads_the_index_and_its_detached_signature() -> None:
    private_pem, public_pem = _keypair()
    raw, sig = _signed(_payload(), private_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".sig"):
            return httpx.Response(200, content=sig)
        return httpx.Response(200, content=raw)

    index = registry.fetch_index(
        "https://example.invalid/index.json",
        keys={KEY_ID: public_pem},
        client=_client(handler),
    )
    assert index.url == "https://example.invalid/index.json"
    assert index.entries[0].name == "genus-hostinfo"


def test_fetch_refuses_an_index_bigger_than_the_cap() -> None:
    _, public_pem = _keypair()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (registry.MAX_INDEX_BYTES + 1))

    with pytest.raises(registry.RegistryError) as excinfo:
        registry.fetch_index(
            "https://example.invalid/index.json",
            keys={KEY_ID: public_pem},
            client=_client(handler),
        )
    assert "too large" in str(excinfo.value)


def test_fetch_refuses_a_redirect_to_another_origin() -> None:
    _, public_pem = _keypair()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.invalid/index.json"})

    with pytest.raises(registry.RegistryError) as excinfo:
        registry.fetch_index(
            "https://example.invalid/index.json",
            keys={KEY_ID: public_pem},
            client=_client(handler),
        )
    assert "redirect" in str(excinfo.value)


def test_fetch_follows_a_same_origin_redirect() -> None:
    private_pem, public_pem = _keypair()
    raw, sig = _signed(_payload(), private_pem)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index.json":
            return httpx.Response(302, headers={"location": "/pages/index.json"})
        if request.url.path.endswith(".sig"):
            return httpx.Response(200, content=sig)
        return httpx.Response(200, content=raw)

    index = registry.fetch_index(
        "https://example.invalid/index.json",
        keys={KEY_ID: public_pem},
        client=_client(handler),
    )
    assert index.entries[0].name == "genus-hostinfo"


def test_fetch_refuses_a_plain_http_url() -> None:
    _, public_pem = _keypair()
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.fetch_index("http://example.invalid/index.json", keys={KEY_ID: public_pem})
    assert "https" in str(excinfo.value)


def test_a_missing_index_is_a_sentence_not_a_traceback() -> None:
    _, public_pem = _keypair()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(registry.RegistryError) as excinfo:
        registry.fetch_index(
            "https://example.invalid/index.json",
            keys={KEY_ID: public_pem},
            client=_client(handler),
        )
    assert "404" in str(excinfo.value)


# --------------------------------------------------------------------------
# selection across indexes
# --------------------------------------------------------------------------


def _index(
    publisher: str, name: str, version: str = "1.0.0", url: str = "https://a.invalid/index.json"
):
    private_pem, public_pem = _keypair()
    payload = _payload(plugins=[dict(_payload()["plugins"][0], name=name, version=version)])
    payload["publisher"] = {"id": publisher, "key_id": KEY_ID}
    raw, sig = _signed(payload, private_pem)
    return registry.parse_index(raw, sig, keys={KEY_ID: public_pem}, url=url)


def test_the_first_index_that_has_the_name_wins() -> None:
    first = _index("genus", "acme-tools", "1.0.0", "https://a.invalid/index.json")
    second = _index("genus", "acme-tools", "2.0.0", "https://b.invalid/index.json")
    entry, index = registry.select("acme-tools", indexes=[first, second])
    assert entry.version == "1.0.0"
    assert index.url == "https://a.invalid/index.json"


def test_two_publishers_offering_one_name_is_refused() -> None:
    first = _index("genus", "acme-tools", "1.0.0", "https://a.invalid/index.json")
    second = _index("someone-else", "acme-tools", "2.0.0", "https://b.invalid/index.json")
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.select("acme-tools", indexes=[first, second])
    assert "--index" in str(excinfo.value)


def test_an_explicit_version_that_is_not_published_is_refused() -> None:
    first = _index("genus", "acme-tools", "1.0.0")
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.select("acme-tools", version="9.9.9", indexes=[first])
    assert "9.9.9" in str(excinfo.value)


def test_a_yanked_entry_is_refused_by_name() -> None:
    private_pem, public_pem = _keypair()
    plugin = dict(_payload()["plugins"][0], yanked=True, yank_reason="key compromise")
    raw, sig = _signed(_payload(plugins=[plugin]), private_pem)
    index = registry.parse_index(raw, sig, keys={KEY_ID: public_pem})
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.select("genus-hostinfo", indexes=[index])
    assert "key compromise" in str(excinfo.value)


def test_a_name_no_index_publishes_is_refused() -> None:
    first = _index("genus", "acme-tools")
    with pytest.raises(registry.RegistryError) as excinfo:
        registry.select("nothing-here", indexes=[first])
    assert "nothing-here" in str(excinfo.value)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def test_the_default_index_is_a_platform_constant_not_a_personal_domain() -> None:
    assert registry.DEFAULT_INDEX_URL == "https://ironsail-llc.github.io/genus-plugins/index.json"


def test_configured_indexes_read_through_the_settings_registry(monkeypatch) -> None:
    from robothor.settings import reset_settings

    monkeypatch.setenv(
        "ROBOTHOR_PLUGIN_INDEXES",
        "https://one.invalid/index.json, https://two.invalid/index.json",
    )
    reset_settings()
    try:
        assert registry.configured_indexes() == (
            "https://one.invalid/index.json",
            "https://two.invalid/index.json",
        )
    finally:
        reset_settings()


def test_publisher_keys_come_from_the_pinned_pem_directory(monkeypatch, tmp_path) -> None:
    from robothor.settings import reset_settings

    _, public_pem = _keypair()
    (tmp_path / "company-key.pem").write_text(public_pem)
    monkeypatch.setenv("ROBOTHOR_PLUGIN_INDEX_KEYS", str(tmp_path))
    reset_settings()
    try:
        keys = registry.publisher_keys()
    finally:
        reset_settings()
    assert keys["company-key"] == public_pem


def test_a_pem_directory_that_holds_junk_does_not_raise(monkeypatch, tmp_path) -> None:
    from robothor.settings import reset_settings

    (tmp_path / "broken.pem").write_text("not a key")
    monkeypatch.setenv("ROBOTHOR_PLUGIN_INDEX_KEYS", str(tmp_path))
    reset_settings()
    try:
        assert registry.publisher_keys() == {}
    finally:
        reset_settings()


def test_the_platform_ships_no_private_key_and_no_placeholder() -> None:
    """The production key is minted by the operator who runs the registry. A
    module shipping one would be a signing key in git."""
    from robothor.plugins import registry_keys

    assert registry_keys.PUBLISHER_KEYS == {}
    source = __import__("pathlib").Path(registry_keys.__file__).read_text()
    assert "PRIVATE KEY" not in source


def test_base64_signature_round_trips() -> None:
    private_pem, public_pem = _keypair()
    payload = _payload()
    doc = json.loads(registry.signature_document(payload, private_pem, key_id=KEY_ID))
    assert doc["key_id"] == KEY_ID
    assert len(base64.b64decode(doc["signature"])) == 64
