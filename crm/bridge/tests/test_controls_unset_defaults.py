"""What the Controls page shows for a flag nobody has written yet.

`list_controls` renders `store.resolve(name) or _default_value_for(name)`, and
`store.resolve` already falls back to the environment — so `_default_value_for`
is reached only when there is neither a DB row nor an env var. In that case the
engine runs the flag's own hardcoded default, and the page must show the same
thing. A page that says `observe` for a flag the engine runs at `enforce` is
worse than no page: the operator reads it as not-yet-on and goes looking for why
it never fired.

The original rule was a heuristic — "boolean → false, anything else → observe" —
with one hand-maintained exception (`ROBOTHOR_DNC_MODE`, which ships enforcing).
It was right for every flag that starts dark and gets promoted, and it silently
became wrong the moment a governed flag shipped at `enforce`
(`ROBOTHOR_PER_USER_SESSIONS`). That is the drift class this file exists to
close: the default is now DERIVED from the settings registry's declared default,
and the test below walks EVERY governed flag rather than the one somebody
remembered.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _norm(value: object) -> str:
    """A flag value as the store and the API spell it."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _engine_readers() -> dict[str, tuple[object, str]]:
    """flag -> (the engine accessor, the env var that gates it, or "").

    Enumerated, not derived, so adding a governed flag stops HERE and is
    considered rather than being silently absent from the comparison. The gate
    matters: a two-var ladder's accessor returns ``off`` while its
    subsystem-enabled var is unset, which is a fact about that OTHER flag, not
    about the value of the one this page is rendering. Setting the gate isolates
    the question to "what does this flag resolve to when nobody has written it".
    """
    from robothor.engine import feature_flags as ff

    return {
        "ROBOTHOR_ADMISSION_MODE": (ff.execution_mode_admission_mode, "ROBOTHOR_ADMISSION_ENABLED"),
        "ROBOTHOR_APPROVAL_MODE": (ff.approval_mode, "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED"),
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE": (
            ff.benchmark_decontamination_mode,
            "ROBOTHOR_BENCHMARK_DECONTAMINATION_ENABLED",
        ),
        "ROBOTHOR_BENCHMARK_SANDBOX_MODE": (
            ff.benchmark_sandbox_mode,
            "ROBOTHOR_BENCHMARK_SANDBOX_ENABLED",
        ),
        "ROBOTHOR_COMPLETION_CONTRACTS_MODE": (
            ff.completion_contract_mode,
            "ROBOTHOR_COMPLETION_CONTRACTS_ENABLED",
        ),
        "ROBOTHOR_DELIVERABLE_CONTRACT_MODE": (
            ff.deliverable_contract_mode,
            "ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED",
        ),
        "ROBOTHOR_DNC_MODE": (ff.do_not_contact_mode, ""),
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE": (
            ff.exec_allowlist_mode,
            "ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED",
        ),
        "ROBOTHOR_HONESTY_SUITE_MODE": (ff.honesty_suite_mode, ""),
        "ROBOTHOR_INJECTION_SCAN_MODE": (ff.injection_scan_mode, "ROBOTHOR_INJECTION_SCAN_ENABLED"),
        "ROBOTHOR_JUDGE_ENABLED": (ff.goal_judge_enabled, ""),
        "ROBOTHOR_PER_USER_SESSIONS": (ff.per_user_sessions_mode, ""),
        "ROBOTHOR_RBAC_MODE": (ff.rbac_enforcement_mode, "ROBOTHOR_RBAC_ENABLED"),
        "ROBOTHOR_RIP_13_MODE": (ff.symbolic_memory_mode, "ROBOTHOR_RIP_13_ENABLED"),
        "ROBOTHOR_RIP_1_ENABLED": (lambda: ff.is_rip_enabled(1), ""),
        "ROBOTHOR_RIP_4_ENABLED": (lambda: ff.is_rip_enabled(4), ""),
        "ROBOTHOR_RIP_5_ENABLED": (ff.curator_enabled, ""),
        "ROBOTHOR_RIP_7_MODE": (ff.rip_7_enforcement_mode, "ROBOTHOR_RIP_7_ENABLED"),
        "ROBOTHOR_RUN_VERIFICATION_MODE": (
            ff.run_verification_mode,
            "ROBOTHOR_RUN_VERIFICATION_ENABLED",
        ),
        "ROBOTHOR_SANDBOX_DEFAULT_MODE": (
            ff.sandbox_default_mode,
            "ROBOTHOR_SANDBOX_DEFAULT_ENABLED",
        ),
        "ROBOTHOR_TOOL_VERIFY_MODE": (ff.tool_verify_mode, "ROBOTHOR_TOOL_VERIFY_ENABLED"),
    }


def test_every_governed_flag_has_an_engine_reader_here():
    """A governed flag nobody mapped is a flag this comparison cannot make."""
    from robothor.flags.store import GOVERNED_FLAGS

    assert set(_engine_readers()) == set(GOVERNED_FLAGS), (
        "a governed flag was added or removed without updating this table — "
        "the page's unset default would then go unchecked against the engine"
    )


@pytest.mark.parametrize("name", sorted(_engine_readers()))
def test_the_displayed_unset_default_is_the_engines_own(name):
    """The one assertion: what the page shows == what the engine runs.

    Both sides are computed with the environment cleared and the store answering
    ``None``, which is exactly the state ``_default_value_for`` exists for.
    """
    from routers.controls import _default_value_for

    from robothor.flags import store

    reader, gate = _engine_readers()[name]
    env = {gate: "1"} if gate else {}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(store, "resolve", return_value=None),
    ):
        engine_default = _norm(reader())
        displayed = _default_value_for(name)

    assert displayed == engine_default, (
        f"{name}: the Controls page would show {displayed!r} while the engine runs "
        f"{engine_default!r} for an unwritten flag"
    )


@pytest.mark.parametrize("name", sorted(_engine_readers()))
def test_the_displayed_unset_default_is_a_value_the_flag_accepts(name):
    """It must also be settable. A default outside ``valid_values`` renders a
    picker whose current selection is not one of its options, and the PATCH that
    would restore it is refused with 422."""
    from routers.controls import _default_value_for

    from robothor.flags import store

    assert _default_value_for(name) in store.valid_values_for(name)


def test_the_page_itself_shows_the_enforcing_default(
    controls_client_as_operator, fake_store, fake_verdict
):
    """End to end, through the response an operator actually reads — not just
    the helper. The two flags that ship enforcing are the ones a heuristic gets
    wrong, so both are named here."""
    response = controls_client_as_operator.get("/api/controls")

    assert response.status_code == 200
    by_name = {flag["name"]: flag for flag in response.json()}
    assert by_name["ROBOTHOR_PER_USER_SESSIONS"]["value"] == "enforce"
    assert by_name["ROBOTHOR_PER_USER_SESSIONS"]["valid_values"] == ["off", "observe", "enforce"]
    assert by_name["ROBOTHOR_DNC_MODE"]["value"] == "enforce"
    # And a flag that genuinely starts dark still reads that way.
    assert by_name["ROBOTHOR_RBAC_MODE"]["value"] == "observe"
    assert by_name["ROBOTHOR_RIP_1_ENABLED"]["value"] == "false"


def test_every_hand_written_override_still_earns_its_place():
    """``_UNSET_DEFAULTS`` is for the flags whose engine default deliberately
    differs from the declared one. An entry that merely repeats the declaration
    is dead weight that will one day be read as the source of truth."""
    from routers.controls import _UNSET_DEFAULTS

    from robothor.settings.registry import field_index

    index = field_index()
    for name, override in _UNSET_DEFAULTS.items():
        assert name in index, f"{name} is overridden here but declared in no settings field"
        declared = _norm(index[name]["default"])
        assert override != declared, (
            f"{name}: the override {override!r} equals the declared default — delete it and let "
            "the registry answer"
        )
