"""Every tool name the engine's own tables carry has to be a real tool.

Four engine tables named tools this platform has never registered:
``gws_calendar_update`` (run_verification, benchmark_sandbox),
``gws_gmail_draft`` (benchmark_sandbox), ``send_email`` (guardrails,
run_verification, and a ranking test that accepted it as a correct answer) —
and ``CORE_TOOLS`` listed ``message``, so the set advertised under deferred
loading was one shorter than the source said it was.

A phantom in an ALLOW table is a tool the agent is told it has. A phantom in a
guardrail table is a guard aimed at nothing: ``_EMAIL_SEND_TOOLS`` contained
``send_email`` and ``send-email``, so the ``inbound_only`` policy matched
neither, while the real route — ``invoke_skill(name="send-email")`` — went
past it untouched. And a phantom anywhere is a name a model reads and calls.
"""

from __future__ import annotations

import pytest

from robothor.engine.tools.constants import (
    CORE_TOOLS,
    GOAL_TOOLS,
    GWS_TOOLS,
    READONLY_TOOLS,
    SPAWN_TOOLS,
    TODO_TOOLS,
    TOOLSEARCH_TOOLS,
    UNREGISTERED_DENY_GUARDS,
)


@pytest.fixture(scope="module")
def schemas() -> set[str]:
    from robothor.engine.tools.registry import builtin_schema_names

    return builtin_schema_names()


@pytest.fixture(scope="module")
def dispatchable(schemas: set[str]) -> set[str]:
    from robothor.engine.tools.dispatch import builtin_handlers

    return schemas | set(builtin_handlers())


def _deny_tables() -> dict[str, frozenset[str]]:
    """Every deny/classification table the engine keeps, found by shape.

    Discovered rather than listed: a hand-maintained list of the tables to
    check is the same kind of second list that let these tables drift in the
    first place.
    """
    import robothor.engine.benchmark_sandbox as benchmark_sandbox
    import robothor.engine.guardrails as guardrails
    import robothor.engine.run_verification as run_verification

    tables: dict[str, frozenset[str]] = {}
    for module in (guardrails, run_verification, benchmark_sandbox):
        for attr in dir(module):
            if not attr.upper().endswith("TOOLS"):
                continue
            value = getattr(module, attr)
            if isinstance(value, frozenset) and all(isinstance(v, str) for v in value):
                tables[f"{module.__name__.rsplit('.', 1)[-1]}.{attr}"] = value
    return tables


# ── Allow tables: the agent is told these exist ───────────────────────


@pytest.mark.parametrize(
    ("label", "table"),
    [
        ("CORE_TOOLS", CORE_TOOLS),
        ("GOAL_TOOLS", GOAL_TOOLS),
        ("SPAWN_TOOLS", SPAWN_TOOLS),
        ("TODO_TOOLS", TODO_TOOLS),
        ("GWS_TOOLS", GWS_TOOLS),
        ("TOOLSEARCH_TOOLS", TOOLSEARCH_TOOLS),
    ],
)
def test_every_advertised_name_has_a_schema(
    label: str, table: frozenset[str], schemas: set[str]
) -> None:
    assert sorted(set(table) - schemas) == [], label


def test_every_readonly_name_is_dispatchable(dispatchable: set[str]) -> None:
    """READONLY_TOOLS classifies rather than advertises — plan mode intersects
    it with the agent's real names — so a handler with no schema is fine here
    and a name with neither is not."""
    assert sorted(set(READONLY_TOOLS) - dispatchable) == []


# ── Deny tables: a guard aimed at a name that cannot appear ───────────


def test_every_deny_table_name_is_real_or_declared(dispatchable: set[str]) -> None:
    undeclared: dict[str, list[str]] = {}
    for label, table in _deny_tables().items():
        missing = sorted(set(table) - dispatchable - UNREGISTERED_DENY_GUARDS)
        if missing:
            undeclared[label] = missing
    assert undeclared == {}, (
        "these deny-list names resolve to no tool; register them, remove them, "
        "or declare them in UNREGISTERED_DENY_GUARDS with a reason"
    )


def test_the_declared_exemptions_are_still_exemptions(dispatchable: set[str]) -> None:
    """The moment one of these becomes a real tool the entry has to go, or the
    exemption quietly starts covering a tool the tables could have checked."""
    stale = sorted(UNREGISTERED_DENY_GUARDS & dispatchable)
    assert stale == [], "these are registered tools now — drop them from UNREGISTERED_DENY_GUARDS"


def test_the_deny_tables_were_actually_found() -> None:
    """A discovery that finds nothing passes every assertion above."""
    tables = _deny_tables()
    assert len(tables) >= 6, sorted(tables)
    assert "guardrails._EMAIL_SEND_TOOLS" in tables


# ── The specific phantoms, named ──────────────────────────────────────


@pytest.mark.parametrize("phantom", ["gws_calendar_update", "gws_gmail_draft", "send_email"])
def test_a_named_phantom_is_gone_from_every_engine_table(
    phantom: str, dispatchable: set[str]
) -> None:
    assert phantom not in dispatchable, "if this is now a real tool, delete this test"
    haunted = [label for label, table in _deny_tables().items() if phantom in table]
    haunted += [
        label
        for label, table in (
            ("CORE_TOOLS", CORE_TOOLS),
            ("READONLY_TOOLS", READONLY_TOOLS),
            ("GWS_TOOLS", GWS_TOOLS),
        )
        if phantom in table
    ]
    assert haunted == []


def test_the_send_email_skill_is_still_guarded(dispatchable: set[str]) -> None:
    """`send_email` left `_EMAIL_SEND_TOOLS`, so the route it was standing in
    for — the skill — has to be covered where it actually travels."""
    from robothor.engine.guardrails import _EMAIL_SEND_SKILLS

    assert "send-email" in _EMAIL_SEND_SKILLS
    assert "invoke_skill" in dispatchable
