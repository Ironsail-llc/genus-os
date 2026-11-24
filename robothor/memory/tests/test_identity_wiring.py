"""Where identity is wired in: the CLI verbs, the nightly pass, the CRM hook seam.

No database here; the behaviour behind each entry point is proved in
``test_identity_integration.py``. These pin that each entry point reaches it,
with the tenant it was given and never a default.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.cli import _build_parser, main

# ─── CLI ─────────────────────────────────────────────────────────────────


def test_memory_identity_sync_parses_and_dispatches() -> None:
    with patch("robothor.cli.memory_identity.cmd_memory_identity", return_value=0) as cmd:
        assert main(["memory-identity", "sync", "--tenant", "test-tenant", "--dry-run"]) == 0
    args = cmd.call_args.args[0]
    assert (args.identity_command, args.tenant, args.dry_run) == ("sync", "test-tenant", True)


def test_memory_identity_backfill_parses_its_flags() -> None:
    args = _build_parser().parse_args(
        ["memory-identity", "backfill", "--tenant", "t1", "--resume-from", "41",
         "--batch-size", "50", "--dry-run"]
    )  # fmt: skip
    assert (args.tenant, args.resume_from, args.batch_size, args.dry_run) == ("t1", 41, 50, True)


def test_memory_identity_backfill_defaults_to_writing() -> None:
    args = _build_parser().parse_args(["memory-identity", "backfill", "--tenant", "t1"])
    assert args.dry_run is False and args.resume_from is None and args.batch_size == 500


def test_memory_identity_reverse_needs_a_decision_and_a_reason() -> None:
    args = _build_parser().parse_args(
        ["memory-identity", "reverse", "--tenant", "t1", "--decision", "7",
         "--reason", "different person", "--entity", "9"]
    )  # fmt: skip
    assert (args.decision, args.reason, args.entity) == (7, "different person", 9)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["memory-identity", "reverse", "--tenant", "t1"])


@pytest.mark.parametrize("verb", ["sync", "backfill", "reverse"])
def test_every_identity_verb_requires_an_explicit_tenant(verb: str) -> None:
    extra = ["--decision", "1", "--reason", "r"] if verb == "reverse" else []
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["memory-identity", verb, *extra])
    assert exc.value.code == 2


def test_the_command_calls_the_module_with_the_tenant(capsys) -> None:
    from robothor.cli import memory_identity

    seen: dict[str, Any] = {}

    def _backfill(tenant_id: str, **kw: Any) -> dict[str, Any]:
        seen.update(tenant_id=tenant_id, **kw)
        return {"tenant_id": tenant_id, "dry_run": True, "by_method": {}, "done": True,
                "last_fact_id": 3, "facts_scanned": 1}  # fmt: skip

    with patch("robothor.memory.identity.backfill_mentions", _backfill):
        rc = memory_identity.cmd_memory_identity(
            _build_parser().parse_args(
                ["memory-identity", "backfill", "--tenant", "t1", "--dry-run", "--json"]
            )
        )
    assert rc == 0
    assert seen == {"tenant_id": "t1", "dry_run": True, "batch_size": 500, "resume_from": None}
    assert '"last_fact_id": 3' in capsys.readouterr().out


# ─── nightly pass ────────────────────────────────────────────────────────


async def test_the_nightly_pass_syncs_every_tenant_and_survives_one_failing() -> None:
    from robothor.memory import lifecycle

    calls: list[str] = []

    def _sync(tenant_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        calls.append(tenant_id)
        if tenant_id == "bad":
            raise RuntimeError("boom")
        return {"people": {"created": 1}, "companies": {"created": 0}}

    with patch("robothor.memory.identity.sync_crm_entities", _sync):
        stats = await lifecycle.sync_crm_identity(["t1", "bad", "t2"])
    assert calls == ["t1", "bad", "t2"]
    assert stats == {"tenants": 2, "failed": 1}


# ─── the CRM hook seam ───────────────────────────────────────────────────


def test_the_crm_hooks_default_to_the_memory_implementation(monkeypatch) -> None:
    from robothor.crm import hooks

    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(hooks, "_crm_entity_sync", None)
    monkeypatch.setattr(hooks, "_crm_merge_recorder", None)
    with (
        patch("robothor.memory.identity.sync_crm_record", lambda *a: seen.append(("sync", *a))),
        patch("robothor.memory.identity.record_crm_merge", lambda *a: seen.append(("merge", *a))),
    ):
        hooks.sync_crm_record("t1", "person", "p1")
        hooks.record_crm_merge("t1", "person", "keep", "lose")
    assert seen == [("sync", "t1", "person", "p1"), ("merge", "t1", "person", "keep", "lose")]


def test_the_crm_hooks_can_be_replaced(monkeypatch) -> None:
    from robothor.crm import hooks

    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(hooks, "_crm_entity_sync", None)
    hooks.register_crm_entity_sync(lambda *a: seen.append(a))
    try:
        hooks.sync_crm_record("t1", "company", "c1")
    finally:
        monkeypatch.setattr(hooks, "_crm_entity_sync", None)
    assert seen == [("t1", "company", "c1")]


def _reverse_cli(argv: list[str]) -> tuple[int, dict[str, Any]]:
    from robothor.cli import memory_identity

    seen: dict[str, Any] = {}

    def _reverse(tenant_id: str, decision_id: int, **kw: Any) -> dict[str, Any]:
        seen.update(tenant_id=tenant_id, decision_id=decision_id, **kw)
        return {
            "new_decision_id": None,
            "mention_ids": [],
            "fact_ids": [],
            "relation_ids": [],
            "dry_run": kw.get("dry_run", False),
        }

    with patch("robothor.memory.identity.reverse_decision", _reverse):
        rc = memory_identity.cmd_memory_identity(_build_parser().parse_args(argv))
    return rc, seen


def test_reverse_on_the_cli_needs_either_facts_or_all() -> None:
    base = ["memory-identity", "reverse", "--tenant", "t1", "--decision", "7", "--reason", "r"]
    rc, seen = _reverse_cli(base)
    assert rc == 2 and seen == {}
    rc, seen = _reverse_cli([*base, "--fact", "3", "--fact", "4", "--entity", "9"])
    assert rc == 0 and seen["fact_ids"] == [3, 4] and seen["new_entity_id"] == 9
    rc, seen = _reverse_cli([*base, "--all", "--dry-run"])
    assert rc == 0 and seen["fact_ids"] is None and seen["dry_run"] is True
    rc, seen = _reverse_cli([*base, "--all", "--fact", "3"])
    assert rc == 2 and seen == {}
