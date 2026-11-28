"""A plugin declares which of its own tools a parent tenant's agent may run here.

The payload key ``remote`` sits beside ``read_only`` and ``effects`` and follows
their rule: a plugin speaks only for tools it provides, and a malformed
declaration refuses the whole plugin, with the tool named, never an exception
out of the loader. The manifest must mirror it as ``remote_tools: {read: [...],
write: [...]}``, so the pre-install scan shows what a wheel exposes, and the
payload must say the same thing. A plugin that says nothing offers nothing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path  # noqa: TC003 - a fixture builds real files
from typing import Any

import pytest

from robothor.plugins import scan, wheel
from robothor.plugins.loader import load_plugins
from robothor.plugins.manifest import parse_manifest


class _Dist:
    def __init__(self, manifest: str | None, name: str = "acme-tools") -> None:
        self._manifest = manifest
        self.metadata = {"Name": name}
        self.name = name

    def read_text(self, filename: str) -> str | None:
        return self._manifest if filename == "genus-plugin.yaml" else None


class _ToolsEP:
    group = "genus.tools"

    def __init__(self, payload: dict[str, Any], manifest: str | None = None) -> None:
        self.name = "acme"
        self.value = "acme_tools:PLUGIN"
        self.dist = _Dist(manifest) if manifest is not None else None
        self._payload = payload

    def load(self) -> dict[str, Any]:
        return self._payload


def _noop(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    return {}


def _payload(remote: Any = None, *, read_only: tuple[str, ...] = ("acme_lookup",)) -> dict:
    payload: dict[str, Any] = {
        "genus_contract_version": "1.0",
        "handlers": {"acme_lookup": _noop, "acme_update": _noop},
        "read_only": list(read_only),
    }
    if remote is not None:
        payload["remote"] = remote
    return payload


def _mirror(remote: Any) -> str:
    """The manifest a well-formed ``remote`` needs: its read and write sets."""
    sets: dict[str, list[str]] = {"read": [], "write": []}
    if isinstance(remote, dict):
        for tool, entry in remote.items():
            access = entry.get("access") if isinstance(entry, dict) else None
            if isinstance(tool, str) and isinstance(access, str) and access in sets:
                sets[access].append(tool)
    return (
        "handlers: [acme_lookup, acme_update]\n"
        f"remote_tools:\n  read: [{', '.join(sets['read'])}]\n"
        f"  write: [{', '.join(sets['write'])}]\n"
    )


def _load(payload: dict[str, Any], manifest: str | None = None):
    """Load one plugin. A payload with ``remote`` gets its manifest mirror
    unless the test passes a manifest of its own."""
    if manifest is None and "remote" in payload:
        manifest = _mirror(payload["remote"])
    return load_plugins(
        entry_points=[_ToolsEP(payload, manifest)], reserved_names=set(), lockfile_path=None
    )


def _refused(result: Any) -> str:
    assert result.failures, "expected the plugin to be refused"
    assert not result.tools, "all or nothing: no tool of a refused plugin registers"
    assert not result.remote
    return " | ".join(f.reason for f in result.failures)


# ── the payload key ──────────────────────────────────────────────────────────


def test_a_declaration_is_loaded_with_its_defaults() -> None:
    result = _load(
        _payload(
            {
                "acme_lookup": {"access": "read", "notes": "Look a record up."},
                "acme_update": {"access": "write", "attestations": ["baa"]},
            }
        )
    )

    assert not result.failures, result.failures
    assert result.remote["acme_lookup"] == {
        "access": "read",
        "attestations": [],
        "notes": "Look a record up.",
        "plugin": "acme-tools",
    }
    assert result.remote["acme_update"]["access"] == "write"
    assert result.remote["acme_update"]["attestations"] == ["baa"]
    assert result.remote["acme_update"]["notes"] == ""


def test_the_plugin_is_named_by_its_distribution_when_it_has_one() -> None:
    result = _load(
        _payload({"acme_lookup": {"access": "read"}}),
        manifest="handlers: [acme_lookup, acme_update]\n",
    )

    assert result.remote["acme_lookup"]["plugin"] == "acme-tools"


def test_absent_key_offers_nothing_and_changes_nothing() -> None:
    result = _load(_payload())

    assert not result.failures
    assert result.remote == {}
    assert set(result.tools) == {"acme_lookup", "acme_update"}
    assert result.read_only == {"acme_lookup"}


def test_a_tool_the_plugin_does_not_provide_is_refused() -> None:
    reasons = _refused(_load(_payload({"send_email": {"access": "read"}})))
    assert "send_email" in reasons


def test_read_on_a_tool_not_in_read_only_is_refused() -> None:
    reasons = _refused(_load(_payload({"acme_update": {"access": "read"}})))
    assert "acme_update" in reasons and "read_only" in reasons


def test_an_unknown_key_in_an_entry_is_refused() -> None:
    reasons = _refused(_load(_payload({"acme_lookup": {"access": "read", "tenant": "x"}})))
    assert "acme_lookup" in reasons


@pytest.mark.parametrize("access", ["READ", "admin", None, "", 1])
def test_a_bad_access_is_refused(access: Any) -> None:
    entry: dict[str, Any] = {} if access is None else {"access": access}
    reasons = _refused(_load(_payload({"acme_lookup": entry})))
    assert "acme_lookup" in reasons


@pytest.mark.parametrize("token", ["BAA", "1baa", "baa-ok", "", "b" * 33, 7])
def test_a_bad_attestation_token_is_refused(token: Any) -> None:
    reasons = _refused(
        _load(_payload({"acme_update": {"access": "write", "attestations": [token]}}))
    )
    assert "acme_update" in reasons


def test_attestations_must_be_a_list() -> None:
    _refused(_load(_payload({"acme_update": {"access": "write", "attestations": "baa"}})))


def test_notes_over_the_limit_are_refused() -> None:
    reasons = _refused(_load(_payload({"acme_lookup": {"access": "read", "notes": "x" * 601}})))
    assert "acme_lookup" in reasons


@pytest.mark.parametrize("bad", [["acme_lookup"], "acme_lookup", {"acme_lookup": "read"}])
def test_a_malformed_declaration_is_refused(bad: Any) -> None:
    _refused(_load(_payload(bad)))


# ── the manifest mirror ──────────────────────────────────────────────────────

_MANIFEST = (
    "name: acme-tools\n"
    "contract_version: 1\n"
    "handlers: [acme_lookup, acme_update]\n"
    "remote_tools:\n"
    "  read: [acme_lookup]\n"
    "  write: [acme_update]\n"
)


def test_the_manifest_key_is_parsed_and_not_mistaken_for_a_contribution_kind() -> None:
    manifest = parse_manifest(_MANIFEST)

    assert manifest is not None
    assert "remote_tools" not in manifest.declared
    assert manifest.remote_tools == {"read": {"acme_lookup"}, "write": {"acme_update"}}


def test_a_manifest_without_the_key_reports_none() -> None:
    manifest = parse_manifest("handlers: [acme_lookup]\n")

    assert manifest is not None and manifest.remote_tools is None


def test_a_payload_matching_the_manifest_loads() -> None:
    result = _load(
        _payload({"acme_lookup": {"access": "read"}, "acme_update": {"access": "write"}}),
        manifest=_MANIFEST,
    )
    assert not result.failures, result.failures
    assert set(result.remote) == {"acme_lookup", "acme_update"}


@pytest.mark.parametrize(
    "remote",
    [
        {"acme_lookup": {"access": "read"}},  # a write the manifest promised is missing
        {"acme_update": {"access": "write"}},  # a read the manifest promised is missing
        None,  # the manifest declares remote tools, the payload none
    ],
)
def test_a_payload_that_differs_from_the_manifest_is_refused(remote: Any) -> None:
    reasons = _refused(_load(_payload(remote), manifest=_MANIFEST))
    assert "remote_tools" in reasons


def test_access_swapped_against_the_manifest_is_refused() -> None:
    manifest = _MANIFEST.replace("read: [acme_lookup]", "read: []").replace(
        "write: [acme_update]", "write: [acme_update, acme_lookup]"
    )
    reasons = _refused(
        _load(
            _payload({"acme_lookup": {"access": "read"}, "acme_update": {"access": "write"}}),
            manifest=manifest,
        )
    )
    assert "acme_lookup" in reasons


def test_a_payload_remote_without_the_manifest_mirror_is_refused() -> None:
    # The pre-install scan reads the manifest only: a wheel exposing tools it
    # does not show there would install looking like it exposes nothing.
    reasons = _refused(
        _load(
            _payload({"acme_lookup": {"access": "read"}}),
            manifest="handlers: [acme_lookup, acme_update]\n",
        )
    )
    assert "remote_tools" in reasons


def test_a_payload_remote_without_any_manifest_is_refused() -> None:
    payload = _payload({"acme_lookup": {"access": "read"}})
    result = load_plugins(
        entry_points=[_ToolsEP(payload, None)], reserved_names=set(), lockfile_path=None
    )

    assert "remote_tools" in _refused(result)


# ── wrong shapes are refusals, never exceptions ──────────────────────────────

_WRONG_SHAPES: list[Any] = [
    # the `remote` value itself
    [],
    "acme_lookup",
    7,
    True,
    # an entry
    {"acme_lookup": ["read"]},
    {"acme_lookup": None},
    {"acme_lookup": 3},
    # access: unhashable and other non-strings
    {"acme_lookup": {"access": ["read"]}},
    {"acme_lookup": {"access": {"read": True}}},
    {"acme_lookup": {"access": {"read"}}},
    {"acme_lookup": {"access": b"read"}},
    {"acme_lookup": {"access": 1.5}},
    # attestations
    {"acme_update": {"access": "write", "attestations": {"baa": True}}},
    {"acme_update": {"access": "write", "attestations": [["baa"]]}},
    {"acme_update": {"access": "write", "attestations": [{"baa": 1}]}},
    {"acme_update": {"access": "write", "attestations": [None]}},
    {"acme_update": {"access": "write", "attestations": 5}},
    # notes
    {"acme_lookup": {"access": "read", "notes": ["x"]}},
    {"acme_lookup": {"access": "read", "notes": {"x": 1}}},
    {"acme_lookup": {"access": "read", "notes": 12}},
    # keys that are not strings, at either level
    {1: {"access": "read"}},
    {("acme_lookup",): {"access": "read"}},
    {"acme_lookup": {1: "read"}},
    {"acme_lookup": {("access",): "read"}},
]


@pytest.mark.parametrize("remote", _WRONG_SHAPES, ids=lambda r: repr(r)[:40])
def test_any_wrong_shape_refuses_the_plugin_cleanly(remote: Any) -> None:
    result = _load(_payload(remote), manifest=_mirror(None))

    _refused(result)


@pytest.mark.parametrize("remote", _WRONG_SHAPES, ids=lambda r: repr(r)[:40])
def test_a_wrong_shape_never_takes_another_plugin_down(remote: Any) -> None:
    class _OtherEP(_ToolsEP):
        def __init__(self) -> None:
            super().__init__(
                {"genus_contract_version": "1.0", "handlers": {"other_tool": _noop}}, None
            )
            self.name = "other"

    result = load_plugins(
        entry_points=[_ToolsEP(_payload(remote), _mirror(None)), _OtherEP()],
        reserved_names=set(),
        lockfile_path=None,
    )

    assert "other_tool" in result.tools
    assert result.failures


# ── the scan reports it ──────────────────────────────────────────────────────

_ENTRY_POINTS = "[genus.tools]\nacme = acme_tools:PLUGIN\n"
_CODE = 'PLUGIN = {"genus_contract_version": "1.0", "handlers": {}}\n'
_METADATA = "Metadata-Version: 2.1\nName: acme-tools\nVersion: 1.2.3\n"


def _scan(tmp_path: Path, manifest: str) -> scan.ScanResult:
    path = tmp_path / "acme_tools-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("acme_tools/__init__.py", _CODE)
        zf.writestr("acme_tools/genus-plugin.yaml", manifest)
        zf.writestr("acme_tools-1.2.3.dist-info/METADATA", _METADATA)
        zf.writestr("acme_tools-1.2.3.dist-info/entry_points.txt", _ENTRY_POINTS)
        zf.writestr("acme_tools-1.2.3.dist-info/WHEEL", "Wheel-Version: 1.0\n")
    return scan.scan_wheel(wheel.open_wheel(path, tmp_path / "x"))


def test_the_scan_reports_the_declared_remote_tools(tmp_path) -> None:
    result = _scan(tmp_path, _MANIFEST + "entry_points:\n  genus.tools: [acme]\n")

    assert result.remote_tools == {"read": ["acme_lookup"], "write": ["acme_update"]}
    assert result.as_json()["remote_tools"] == {"read": ["acme_lookup"], "write": ["acme_update"]}
    assert result.verdict == "safe", result.reasons


def test_the_scan_reports_none_when_the_manifest_declares_none(tmp_path) -> None:
    result = _scan(tmp_path, "contract_version: 1\nhandlers: [acme_lookup]\n")

    assert result.remote_tools is None
    assert "remote_tools" not in result.as_json()  # serialised exactly as before


@pytest.mark.parametrize(
    "block",
    [
        "remote_tools: [acme_lookup]\n",  # a list, not a dict
        "remote_tools:\n  read: [acme_lookup]\n  admin: [acme_update]\n",  # unknown key
        "remote_tools:\n  read: [acme_lookup]\n  write: [acme_lookup]\n",  # both
        "remote_tools:\n  read: [somebody_elses_tool]\n",  # not a declared tool
    ],
)
def test_the_scan_blocks_a_malformed_mirror(tmp_path, block: str) -> None:
    manifest = "contract_version: 1\nhandlers: [acme_lookup, acme_update]\n" + block
    result = _scan(tmp_path, manifest)

    assert result.verdict == "blocked"
    assert any("remote_tools" in reason for reason in result.reasons), result.reasons
