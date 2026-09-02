"""`infra/systemd/robothor.env.example` is the template for the one file no
gate in CI can see.

`/etc/robothor/robothor.env` is instance data — secrets, tenant ids, paths —
so it is not in git and never will be. systemd applies it via
`EnvironmentFile=` AFTER the drop-in's `Environment=` directives, which means a
guardrail set there wins outright, and the mirror test in
`tests/test_flag_manifest.py` (which reads only the drop-in) cannot see it.

On 2026-09-02 three controls were found living only there:
completion-contracts at `enforce`, admission at `enforce`, the deliverable
contract at `observe`. `infra/flags.yaml` said admission was in `observe`; it
had been enforcing since 2026-08-27. Nothing in git recorded any of it and a
rebuilt instance would have come up without all three.

The example file is where that rule is taught, so these tests hold it to two
things: it must never SET a governed flag itself, and it must SAY why. Plus the
knobs an operator cannot otherwise discover — the sibling rule in
`robothor/engine/tests/test_execution_mode_knobs_documented.py`, applied to the
keys this reconciliation found missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The canonical instance env template. Asserted here and in
#: robothor/engine/tests/test_execution_mode_knobs_documented.py:17.
CANONICAL = REPO_ROOT / "infra" / "systemd" / "robothor.env.example"

#: The older, top-level copy. Two example files diverging is how an operator
#: fills in the wrong one, so it must point at the canonical file rather than
#: compete with it.
LEGACY = REPO_ROOT / "infra" / "robothor.env.example"

#: Knobs read by code but documented nowhere an operator would look, found by
#: the 2026-09-02 flag/env reconciliation. Each is commented out in the
#: example: documenting a knob is not the same as setting it.
REQUIRED_KNOBS = (
    # resume of in-flight runs across a restart
    "ROBOTHOR_RESUME_IN_FLIGHT",
    # the deliverable contract (docs/runbooks/DELIVERABLE_CONTRACT.md)
    "ROBOTHOR_DELIVERABLE_CONTRACT_ENABLED",
    "ROBOTHOR_DELIVERABLE_CONTRACT_MODE",
    # makes sandbox `enforce` actually govern manifest opt-outs
    "ROBOTHOR_SANDBOX_ENFORCE_OVERRIDES_MANIFEST",
    # the role a service principal gets when none is assigned
    "ROBOTHOR_DEFAULT_SERVICE_ROLE",
    # the global kill switch — documented precisely because it is dangerous
    "ROBOTHOR_DISABLE_ALL_GUARDRAILS",
    # credential pool cooldown after a periodic (weekly/monthly) quota cap
    "ROBOTHOR_PERIODIC_QUOTA_COOLDOWN_SECONDS",
    # how long a paced local request waits for the box to cool
    "ROBOTHOR_LOCAL_GATE_WAIT_SECONDS",
    # plugin manifest admission ladder
    "ROBOTHOR_PLUGIN_MANIFEST_ENABLED",
    "ROBOTHOR_PLUGIN_MANIFEST_MODE",
    # federation broker credentials + the RLS escape hatch
    "ROBOTHOR_NATS_URL",
    "ROBOTHOR_NATS_USER",
    "ROBOTHOR_NATS_PASSWORD",
    "ROBOTHOR_NATS_CONFIG",
    "ROBOTHOR_FEDERATION_ALLOW_INERT_RLS",
)


def active_assignments(text: str) -> dict[str, str]:
    """`NAME=VALUE` lines systemd would actually apply.

    Parsed the way systemd parses an EnvironmentFile — `#` comments, no shell
    evaluation — and the way `scripts/flag_audit.py: parse_env_file` does, so
    "this test sees what the audit sees" is true rather than hoped.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name or not name.replace("_", "").isalnum():
            continue
        out[name] = value.strip().strip('"').strip("'")
    return out


def test_active_assignments_ignores_comments_and_reads_real_lines():
    parsed = active_assignments("# A=1\nB=2\n\n  # C=3\nexport D=4\nnot a line\n")
    assert parsed == {"B": "2", "D": "4"}


# ── The rule ────────────────────────────────────────────────────────────────


def posture_flag_names() -> set[str]:
    """Every guardrail-ladder variable, derived rather than hand-listed.

    `GOVERNED_FLAGS` names the `*_MODE` half; the `*_ENABLED` companion that
    gates each one is read out of `feature_flags.py` by the same AST walk the
    flag audit uses. A parallel list here would drift from the code that reads
    the flags — and a guard on a stale list is the shape of defect this whole
    reconciliation exists to close. A name-suffix heuristic is no good either:
    `ROBOTHOR_VISION_MODE` is an operating mode, not a guardrail.
    """
    import importlib.util
    import sys

    from robothor.flags.store import GOVERNED_FLAGS

    flag_audit = sys.modules.get("flag_audit")
    if flag_audit is None:
        spec = importlib.util.spec_from_file_location(
            "flag_audit", REPO_ROOT / "scripts" / "flag_audit.py"
        )
        assert spec is not None and spec.loader is not None
        flag_audit = importlib.util.module_from_spec(spec)
        # Registered BEFORE exec: @dataclass resolves its own module out of
        # sys.modules, and a spec-loaded script that never lands there raises
        # on the first one (same note as tests/test_flag_audit.py).
        sys.modules["flag_audit"] = flag_audit
        spec.loader.exec_module(flag_audit)

    gates = flag_audit.mode_gate_map()
    names = set(GOVERNED_FLAGS) | set(gates)
    names |= {g.enabled_var for g in gates.values() if g.enabled_var}
    return names


def test_posture_flag_names_covers_the_ladder_but_not_operating_modes():
    names = posture_flag_names()
    assert "ROBOTHOR_ADMISSION_MODE" in names
    assert "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED" in names  # not *_APPROVAL_ENABLED
    assert "ROBOTHOR_VISION_MODE" not in names


def test_env_file_example_sets_no_governed_mode_flags():
    """The env file must not carry guardrail posture at all.

    Not "must agree with the drop-in" — must not be there. A name in both
    files is the noisy case that `check_dropin_drift.sh` already catches; a
    name ONLY here is the dangerous one, invisible to every gate in CI.
    """
    posture = posture_flag_names()
    for path in (CANONICAL, LEGACY):
        if not path.exists():
            continue
        active = sorted(set(active_assignments(path.read_text())) & posture)
        assert not active, (
            f"{path.name} SETS guardrail flags {active}. Posture belongs in "
            "infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf, "
            "which is versioned and drift-checked, and in infra/flags.yaml. "
            "Comment the line out."
        )


def test_env_file_example_says_why_posture_belongs_in_the_dropin():
    """A commented-out line records nothing on its own. The reason has to be
    written where the operator is editing, or the next person uncomments it."""
    text = CANONICAL.read_text()
    assert "upgrade-rip-flags.conf" in text, (
        "the example never names the drop-in, so nothing tells the operator "
        "where guardrail posture actually belongs"
    )
    assert "GUARDRAIL_FLIPS" in text, "the example does not point at the flip runbook"


@pytest.mark.parametrize("knob", REQUIRED_KNOBS)
def test_reconciliation_knobs_are_documented(knob: str):
    """A knob an operator cannot discover is a knob that does not exist for
    them. Each of these is read by code and appeared in no example file."""
    assert knob in CANONICAL.read_text(), (
        f"{knob} is read by the engine but appears in no env example; "
        "an operator on a new box cannot discover it"
    )


def test_legacy_example_points_at_the_canonical_one():
    """Two example files that drift is how an operator fills in the wrong one."""
    if not LEGACY.exists():
        pytest.skip("no top-level example file")
    text = LEGACY.read_text()
    assert "infra/systemd/robothor.env.example" in text, (
        "infra/robothor.env.example does not name the canonical template"
    )
