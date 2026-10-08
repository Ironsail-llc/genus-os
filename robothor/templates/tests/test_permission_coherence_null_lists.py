"""Check H must accept a manifest whose tool lists are present but empty.

2026-10-07. ``tools_denied:`` with nothing under it is YAML ``null``, not ``[]``.
Four shipped templates (email-classifier, crm-steward, conversation-inbox,
conversation-resolver) carry exactly that, and ``set(None)`` made check H raise
``TypeError`` — so ``genus agent install`` crashed on them after rendering.
"""

from __future__ import annotations

from robothor.templates.manifest_checks import check_permission_coherence


def test_null_tools_denied_is_an_empty_list() -> None:
    result = check_permission_coherence({"tools_allowed": ["read_file"], "tools_denied": None})
    assert result.status == "PASS"


def test_null_tools_allowed_is_an_empty_list() -> None:
    result = check_permission_coherence({"tools_allowed": None, "tools_denied": ["exec"]})
    assert result.status == "PASS"


def test_overlap_still_warns() -> None:
    result = check_permission_coherence({"tools_allowed": ["exec"], "tools_denied": ["exec"]})
    assert result.status == "WARN"
