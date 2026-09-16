"""One registry for governed flags: the settings model.

Before this, a governed guardrail flag existed in three places that had never
been compared: the hand-written ``GOVERNED_FLAGS`` set in
``robothor/flags/store.py`` (what the Controls API can show and set, and what
the engine resolves from the DB), ``infra/flags.yaml`` (who owns it, what mode
production runs, when it is due for promotion), and -- since the settings model
landed -- a ``governed=True`` field declaration. They disagreed: sixteen of the
twenty names in ``GOVERNED_FLAGS`` were declared nowhere in the model, and
fourteen fields marked ``governed=True`` were in neither the store nor the
manifest.

``GOVERNED_FLAGS`` now derives from the model, so the only way to govern a flag
is to declare it, and these tests hold the three lists to each other.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "infra" / "flags.yaml"

#: The hand-written set ``robothor.flags.store`` carried until this PR, pinned
#: here so the derivation can be proven to reproduce it exactly. A flag leaving
#: governance must delete its name from this constant deliberately -- silently
#: dropping one is how a control stops being reachable from the dashboard while
#: everything still looks wired.
HISTORICAL_GOVERNED_FLAGS: frozenset[str] = frozenset(
    {
        "ROBOTHOR_RBAC_MODE",
        "ROBOTHOR_INJECTION_SCAN_MODE",
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE",
        "ROBOTHOR_APPROVAL_MODE",
        "ROBOTHOR_SANDBOX_DEFAULT_MODE",
        "ROBOTHOR_ADMISSION_MODE",
        "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
        "ROBOTHOR_RIP_7_MODE",
        "ROBOTHOR_RIP_13_MODE",
        "ROBOTHOR_RIP_1_ENABLED",
        "ROBOTHOR_RIP_4_ENABLED",
        "ROBOTHOR_RIP_5_ENABLED",
        "ROBOTHOR_JUDGE_ENABLED",
        "ROBOTHOR_RUN_VERIFICATION_MODE",
        "ROBOTHOR_TOOL_VERIFY_MODE",
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE",
        "ROBOTHOR_DELIVERABLE_CONTRACT_MODE",
        "ROBOTHOR_HONESTY_SUITE_MODE",
        "ROBOTHOR_BENCHMARK_SANDBOX_MODE",
        "ROBOTHOR_DNC_MODE",
        # Added deliberately when webchat became a delivery destination: a
        # shared chat session stopped being only a privacy smell and became a
        # place one member's delivery could land where another reads. Governing
        # it means an operator can move the rung from Controls instead of
        # editing /etc and restarting — which is what you want of a switch that
        # decides who can read whose conversation.
        "ROBOTHOR_PER_USER_SESSIONS",
        # Added 2026-09-15 with the step-efficiency controls. Governed from the
        # start rather than after the fact: the rung it sits on decides whether
        # a run's `exec` can be refused mid-task, and an operator watching a
        # sweep needs to move that from Controls, not from /etc plus a restart.
        "ROBOTHOR_STEP_EFFICIENCY_MODE",
        # Added 2026-09-16 after the assistant wrote the operator's itinerary to
        # its OWN calendar and told him it was on his. Not a ladder — the values
        # are all/externalOnly/none — but governed, because "stop emailing my
        # attendees" is a posture an operator must be able to take from Controls
        # rather than by editing a box, and because the state it replaces was an
        # effective `none` that nobody chose and nobody could see.
        "ROBOTHOR_CALENDAR_SEND_UPDATES",
    }
)

#: Manifest entries that are deliberately NOT DB-governed, with the reason.
#: ``infra/flags.yaml`` inventories rollout intent; ``GOVERNED_FLAGS`` is the
#: narrower set the DB store can actually decide. A name here is in the
#: inventory but is read on a path the store cannot reach, so adding it to
#: ``GOVERNED_FLAGS`` would give an operator a switch that flips a row nothing
#: reads -- an inert control, which is worse than no control.
MANIFEST_ONLY_FLAGS: dict[str, str] = {
    "ROBOTHOR_CONFIG_STRICT_MODE": (
        "read by robothor.settings.sources while resolving settings, which runs "
        "before (and without) a database"
    ),
    "ROBOTHOR_PLUGIN_MANIFEST_MODE": (
        "read by robothor/plugins/manifest.py straight from os.environ; route it "
        "through feature_flags first, then govern it"
    ),
    "ROBOTHOR_MANIFEST_SCHEMA_MODE": (
        "read by robothor/engine/manifest_schema.py straight from os.environ; "
        "route it through feature_flags first, then govern it"
    ),
}


def _derived() -> set[str]:
    from robothor.settings.registry import field_index

    return {record["env"] for record in field_index().values() if record["governed"]}


def _manifest_names() -> set[str]:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return {entry["name"] for entry in data["flags"]}


def test_governed_flags_derive_from_the_settings_registry() -> None:
    from robothor.flags.store import GOVERNED_FLAGS

    assert set(GOVERNED_FLAGS) == _derived()


def test_derivation_reproduces_the_hand_written_list() -> None:
    """No flag gained or lost governance in the move to a derived set."""
    assert _derived() == set(HISTORICAL_GOVERNED_FLAGS)


def test_every_governed_field_is_inventoried_in_the_manifest() -> None:
    missing = sorted(_derived() - _manifest_names())
    assert not missing, (
        f"governed=True but absent from infra/flags.yaml: {missing}. A governed "
        "flag with no owner and no promotion date is a flag nobody is watching."
    )


def test_every_manifest_entry_maps_to_a_declared_field() -> None:
    from robothor.settings.registry import field_index

    index = field_index()
    undeclared = sorted(name for name in _manifest_names() if name not in index)
    assert not undeclared, (
        f"inventoried in infra/flags.yaml but declared in no settings field: "
        f"{undeclared}. Declare it in robothor/settings/model.py."
    )


def test_manifest_entries_outside_governance_are_the_documented_ones() -> None:
    assert _manifest_names() - _derived() == set(MANIFEST_ONLY_FLAGS)


def test_every_governed_flag_has_valid_values() -> None:
    """The Controls API offers ``valid_values`` for whatever it can set."""
    from robothor.flags.store import valid_values_for

    for name in _derived():
        assert valid_values_for(name), name


def test_set_flag_refuses_an_ungoverned_name() -> None:
    import pytest

    from robothor.flags.store import set_flag

    with pytest.raises(ValueError, match="not a governed flag"):
        set_flag("ROBOTHOR_NOT_A_FLAG", "enforce", "test", "test")


def test_importing_the_store_does_not_load_pydantic() -> None:
    """``robothor.flags.store`` is imported on the engine's hot path.

    Deriving the set from the model must stay lazy: a module-level derivation
    would make every process that resolves a flag pay for pydantic-settings.
    """
    import subprocess
    import sys

    code = "import sys, robothor.flags.store; print('pydantic_settings' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "False"
