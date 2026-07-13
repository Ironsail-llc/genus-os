"""Adversarial tests for remote agent bundle download and extraction."""

from __future__ import annotations

import hashlib
import io
import tarfile
from unittest.mock import Mock

import httpx
import pytest

from robothor.templates.hub_client import HubClient, HubError, trusted_bundle_sha256


def _archive(*members: tuple[str, bytes | None, bytes | None]) -> bytes:
    """Build a gzip tar. Third tuple item is a symlink target when non-None."""

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content, link_target in members:
            info = tarfile.TarInfo(name)
            if link_target is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link_target.decode()
                archive.addfile(info)
            else:
                payload = content or b""
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        request=httpx.Request("GET", "https://hub.example/download"),
    )


def _client_with_download(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> tuple[HubClient, Mock]:
    client = HubClient(api_key="test", base_url="https://hub.example")
    request = Mock(return_value=_response(content))
    monkeypatch.setattr(client, "_request_with_retry", request)
    return client, request


def test_trusted_registry_digest_is_mandatory_and_strict() -> None:
    digest = "a" * 64
    assert trusted_bundle_sha256({"sha256": digest}) == digest

    for metadata in ({}, {"sha256": "A" * 64}, {"sha256": "short"}, None):
        with pytest.raises(HubError, match="valid SHA-256"):
            trusted_bundle_sha256(metadata)


@pytest.mark.parametrize("slug", ["../escape", "/absolute", "Uppercase", "a/b", "."])
def test_download_rejects_unsafe_slug_before_network(
    monkeypatch: pytest.MonkeyPatch, slug: str
) -> None:
    content = _archive(("bundle/setup.yaml", b"agent_id: bundle\n", None))
    client, request = _client_with_download(monkeypatch, content)

    with pytest.raises(HubError, match="slug"):
        client.download_bundle(slug, expected_sha256=hashlib.sha256(content).hexdigest())
    request.assert_not_called()


@pytest.mark.parametrize("expected", [None, "", "f" * 63, "F" * 64])
def test_download_rejects_missing_or_malformed_checksum_before_network(
    monkeypatch: pytest.MonkeyPatch, expected: str | None
) -> None:
    content = _archive(("bundle/setup.yaml", b"agent_id: bundle\n", None))
    client, request = _client_with_download(monkeypatch, content)

    with pytest.raises(HubError, match="trusted SHA-256"):
        client.download_bundle("bundle", expected_sha256=expected)
    request.assert_not_called()


def test_download_rejects_wrong_checksum(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    content = _archive(("bundle/setup.yaml", b"agent_id: bundle\n", None))
    client, _ = _client_with_download(monkeypatch, content)

    with pytest.raises(HubError, match="Checksum verification failed"):
        client.download_bundle("bundle", expected_sha256="0" * 64, dest_dir=tmp_path / "downloads")
    assert not (tmp_path / "downloads").exists()


@pytest.mark.parametrize("member", ["../escape", "/tmp/escape", "bundle/../../escape"])
def test_download_rejects_archive_path_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path, member: str
) -> None:
    content = _archive(
        ("bundle/setup.yaml", b"agent_id: bundle\n", None),
        (member, b"malicious", None),
    )
    client, _ = _client_with_download(monkeypatch, content)

    with pytest.raises(HubError, match="Unsafe bundle archive member"):
        client.download_bundle(
            "bundle",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            dest_dir=tmp_path / "downloads",
        )
    assert not (tmp_path / "escape").exists()


def test_download_rejects_archive_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    content = _archive(
        ("bundle/setup.yaml", b"agent_id: bundle\n", None),
        ("bundle/instructions.md", None, b"../../outside"),
    )
    client, _ = _client_with_download(monkeypatch, content)

    with pytest.raises(HubError, match="Unsupported bundle archive member"):
        client.download_bundle(
            "bundle",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            dest_dir=tmp_path / "downloads",
        )
    assert not (tmp_path / "outside").exists()


def test_download_rejects_symlink_destination(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    content = _archive(("bundle/setup.yaml", b"agent_id: bundle\n", None))
    client, _ = _client_with_download(monkeypatch, content)
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "downloads"
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HubError, match="destination cannot be a symlink"):
        client.download_bundle(
            "bundle",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            dest_dir=destination,
        )
    assert list(outside.iterdir()) == []


def test_download_extracts_verified_regular_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    content = _archive(
        ("bundle/setup.yaml", b"agent_id: bundle\n", None),
        ("bundle/manifest.template.yaml", b"id: bundle\n", None),
    )
    client, _ = _client_with_download(monkeypatch, content)

    bundle = client.download_bundle(
        "bundle",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        dest_dir=tmp_path / "downloads",
    )

    assert bundle.name == "bundle"
    assert (bundle / "setup.yaml").read_text() == "agent_id: bundle\n"
