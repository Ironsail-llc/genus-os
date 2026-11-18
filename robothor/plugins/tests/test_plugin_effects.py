"""A plugin declares, for its own write tools, what each acts on and how to read it back.

Same seam and same rules as ``read_only``: a plugin speaks only for tools it
provides, and a malformed declaration refuses the whole plugin.
"""

from __future__ import annotations

from typing import Any

from robothor.plugins.loader import load_plugins


def _verify(record: dict[str, Any]) -> Any:
    return None


class _ToolsEP:
    """The smallest entry point the loader accepts, as the other plugin tests use."""

    group = "genus.tools"

    def __init__(self, payload: dict[str, Any], name: str = "acme") -> None:
        self.name = name
        self.value = f"{name}:PLUGIN"
        self.dist = None
        self._payload = payload

    def load(self) -> dict[str, Any]:
        return self._payload


def _load(monkeypatch, payload: dict[str, Any]):
    return load_plugins(entry_points=[_ToolsEP(payload)], reserved_names=set())


def _payload(effects: Any) -> dict[str, Any]:
    return {
        "genus_contract_version": "1.0",
        "handlers": {"acme_post_message": lambda args, ctx=None: {}},
        "effects": effects,
    }


def test_a_declaration_is_loaded(monkeypatch):
    result = _load(
        monkeypatch,
        _payload({"acme_post_message": {"target": ["thread_id"], "verify": _verify}}),
    )
    assert not result.failures, result.failures
    spec = result.effects["acme_post_message"]
    assert spec.target == ("thread_id",) and spec.verify is _verify and spec.fence == "target"


def test_a_tool_the_plugin_does_not_provide_is_refused(monkeypatch):
    result = _load(monkeypatch, _payload({"send_email": {"verify": _verify}}))
    assert "send_email" not in result.effects
    assert any("not provided by this plugin" in f.reason for f in result.failures)
    assert "acme_post_message" not in result.tools  # all or nothing


def test_a_malformed_declaration_is_refused(monkeypatch):
    for bad in (
        {"acme_post_message": {"verify": "not callable"}},
        {"acme_post_message": {"fence": "everything"}},
        {"acme_post_message": {"target": "thread_id"}},
        {"acme_post_message": {"target": []}},
        {"acme_post_message": {"unknown_key": 1}},
        ["acme_post_message"],
    ):
        result = _load(monkeypatch, _payload(bad))
        assert result.failures and not result.effects, bad
