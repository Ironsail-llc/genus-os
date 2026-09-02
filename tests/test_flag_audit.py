"""Tests for scripts/flag_audit.py — the flag truth table as a command.

Three layers already disagree about what this fleet's guardrails are doing:
``infra/flags.yaml`` records intent, the systemd drop-in and
``/etc/robothor/robothor.env`` both set ``Environment=``-style values (and the
env file is applied AFTER the drop-in, so it silently wins), and a
``feature_flags`` DB row beats both (robothor/flags/store.py). Nothing printed
the effective state from the artifact that actually executes — the engine's
own ``/proc/<pid>/environ``.

Every input here is injected: a fake NUL-separated environ blob, a fake env
file, a fake drop-in directory, a fake manifest, and a stubbed DB state. The
audit is read-only by design, so no test needs /etc, systemd or a database.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "flag_audit", REPO_ROOT / "scripts" / "flag_audit.py"
)
assert _spec is not None and _spec.loader is not None
fa = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves its own module out of sys.modules,
# and a spec-loaded script that never lands there raises on the first one.
sys.modules["flag_audit"] = fa
_spec.loader.exec_module(fa)

TODAY = dt.date(2026, 9, 2)


# --- layer parsers ----------------------------------------------------------


def test_parse_environ_blob_splits_on_nul():
    blob = b"PATH=/usr/bin\x00ROBOTHOR_ADMISSION_MODE=enforce\x00LANG=C.UTF-8\x00"
    env = fa.parse_environ_blob(blob)
    assert env["ROBOTHOR_ADMISSION_MODE"] == "enforce"
    assert env["PATH"] == "/usr/bin"


def test_parse_environ_blob_keeps_values_containing_equals():
    env = fa.parse_environ_blob(b"DSN=postgres://u:p@h/db?a=b\x00")
    assert env["DSN"] == "postgres://u:p@h/db?a=b"


def test_parse_env_file_ignores_comments_blanks_and_export():
    text = (
        "# a comment\n"
        "\n"
        "ROBOTHOR_ADMISSION_MODE=enforce\n"
        "export ROBOTHOR_ADMISSION_ENABLED=1\n"
        '  ROBOTHOR_QUOTED="observe"\n'
        "not a flag line\n"
    )
    parsed = fa.parse_env_file(text)
    assert parsed == {
        "ROBOTHOR_ADMISSION_MODE": "enforce",
        "ROBOTHOR_ADMISSION_ENABLED": "1",
        "ROBOTHOR_QUOTED": "observe",
    }


def test_parse_dropin_dir_reads_only_conf_files(tmp_path):
    (tmp_path / "a.conf").write_text(
        "[Service]\nEnvironment=ROBOTHOR_RBAC_MODE=enforce\nEnvironment=ROBOTHOR_RBAC_ENABLED=1\n"
    )
    # systemd loads *.conf only — a .bak file left beside it is NOT live state,
    # and reading it would invent a layer the engine never saw.
    (tmp_path / "a.conf.bak-1234").write_text("Environment=ROBOTHOR_RBAC_MODE=off\n")
    parsed = fa.parse_dropin_dir(tmp_path)
    assert parsed == {"ROBOTHOR_RBAC_MODE": "enforce", "ROBOTHOR_RBAC_ENABLED": "1"}


def test_parse_dropin_dir_missing_dir_is_empty():
    assert fa.parse_dropin_dir(Path("/nonexistent/drop-in/dir")) == {}


# --- the ENABLED/MODE gate map is derived, never hand-maintained ------------


def test_mode_gate_map_is_parsed_from_feature_flags_source():
    """The ENABLED companion of each *_MODE flag is read out of
    feature_flags.py itself.

    A hand-written parallel list is how ROBOTHOR_APPROVAL_MODE would silently
    get paired with a nonexistent ROBOTHOR_APPROVAL_ENABLED instead of its real
    gate, ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED.
    """
    gates = fa.mode_gate_map()
    assert gates["ROBOTHOR_APPROVAL_MODE"].enabled_var == "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED"
    assert gates["ROBOTHOR_ADMISSION_MODE"].enabled_var == "ROBOTHOR_ADMISSION_ENABLED"
    assert gates["ROBOTHOR_RIP_7_MODE"].enabled_var == "ROBOTHOR_RIP_7_ENABLED"
    # The honesty suite is gated on its MODE alone and defaults to observe.
    assert gates["ROBOTHOR_HONESTY_SUITE_MODE"].enabled_var is None
    assert gates["ROBOTHOR_HONESTY_SUITE_MODE"].default == "observe"


# --- effective value: _enforcement_mode semantics ---------------------------


def test_effective_is_off_when_the_enabled_companion_is_falsy():
    gates = fa.mode_gate_map()
    resolved = {"ROBOTHOR_ADMISSION_MODE": "enforce"}  # no *_ENABLED at all
    assert fa.effective_value("ROBOTHOR_ADMISSION_MODE", resolved, gates) == "off"


def test_effective_is_observe_when_enabled_with_no_mode_set():
    gates = fa.mode_gate_map()
    resolved = {"ROBOTHOR_ADMISSION_ENABLED": "1"}
    assert fa.effective_value("ROBOTHOR_ADMISSION_MODE", resolved, gates) == "observe"


def test_boolean_flag_effective_is_true_false():
    gates = fa.mode_gate_map()
    assert fa.effective_value("ROBOTHOR_JUDGE_ENABLED", {"ROBOTHOR_JUDGE_ENABLED": "1"}, gates) == (
        "true"
    )
    assert fa.effective_value("ROBOTHOR_JUDGE_ENABLED", {}, gates) == "false"


def test_panic_flag_forces_every_flag_off():
    gates = fa.mode_gate_map()
    resolved = {
        "ROBOTHOR_DISABLE_ALL_RIPS": "1",
        "ROBOTHOR_ADMISSION_ENABLED": "1",
        "ROBOTHOR_ADMISSION_MODE": "enforce",
    }
    assert fa.effective_value("ROBOTHOR_ADMISSION_MODE", resolved, gates) == "off"


# --- the whole audit --------------------------------------------------------


def _manifest(tmp_path, entries):
    path = tmp_path / "flags.yaml"
    body = "flags:\n"
    for e in entries:
        body += f'  - name: {e["name"]}\n    owner: ops\n    mode: "{e["mode"]}"\n'
        if e.get("planned_promotion"):
            body += f'    planned_promotion: "{e["planned_promotion"]}"\n'
        body += "    soak: s\n"
    path.write_text(body)
    return path


def _dropin(tmp_path, lines):
    d = tmp_path / "dropin"
    d.mkdir()
    (d / "flags.conf").write_text("[Service]\n" + "".join(f"Environment={x}\n" for x in lines))
    return d


def test_env_file_wins_over_dropin_and_mismatches_the_manifest(tmp_path):
    """The live instance's ADMISSION story, reproduced from injected inputs.

    flags.yaml says observe. The drop-in says nothing about admission. The env
    file — applied by systemd AFTER the drop-in's Environment= directives —
    sets enforce, and that is what the running process carries.
    """
    manifest = _manifest(
        tmp_path,
        [{"name": "ROBOTHOR_ADMISSION_MODE", "mode": "observe", "planned_promotion": None}],
    )
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_ADMISSION_ENABLED=1\nROBOTHOR_ADMISSION_MODE=enforce\n")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_ADMISSION_ENABLED=1\x00ROBOTHOR_ADMISSION_MODE=enforce\x00")

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "no-dropins",
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_ADMISSION_MODE")

    assert row.yaml_mode == "observe"
    assert row.envfile == "enforce"
    assert row.dropin is None
    assert row.effective == "enforce"
    assert row.layer == "envfile"
    assert "MISMATCH" in row.tags


def test_flag_set_in_both_env_file_and_dropin_is_a_shadow_layer(tmp_path):
    """A drop-in edit that the env file overrides is a flip that does nothing —
    the 2026-07-25 router revert, which check_dropin_drift.sh reported as OK.
    """
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_RBAC_ENABLED=1\nROBOTHOR_RBAC_MODE=enforce\n")
    dropin = _dropin(tmp_path, ["ROBOTHOR_RBAC_ENABLED=1", "ROBOTHOR_RBAC_MODE=observe"])
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_ENABLED=1\x00ROBOTHOR_RBAC_MODE=enforce\x00")

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=dropin,
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")

    assert row.dropin == "observe"
    assert row.envfile == "enforce"
    assert row.layer == "envfile"
    assert "SHADOW-LAYER:envfile" in row.tags
    # The manifest and the running process agree, so this is shadowing only.
    assert "MISMATCH" not in row.tags


def test_db_pin_beats_every_file_layer(tmp_path):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_RBAC_ENABLED=1\nROBOTHOR_RBAC_MODE=enforce\n")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_ENABLED=1\x00ROBOTHOR_RBAC_MODE=enforce\x00")
    db = fa.DbState(pins={"ROBOTHOR_RBAC_MODE": "observe"}, evidence={})

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=db,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")

    assert row.db_pin == "observe"
    assert row.effective == "observe"
    assert row.layer == "db"
    assert "SHADOW-LAYER:db" in row.tags
    assert "MISMATCH" in row.tags  # manifest still claims enforce


def test_overdue_tag_matches_the_manifest_deadline(tmp_path):
    manifest = _manifest(
        tmp_path,
        [
            {
                "name": "ROBOTHOR_SANDBOX_DEFAULT_MODE",
                "mode": "observe",
                "planned_promotion": "2026-08-01",
            }
        ],
    )
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    dropin = _dropin(
        tmp_path, ["ROBOTHOR_SANDBOX_DEFAULT_ENABLED=1", "ROBOTHOR_SANDBOX_DEFAULT_MODE=observe"]
    )
    environ = tmp_path / "environ"
    environ.write_bytes(
        b"ROBOTHOR_SANDBOX_DEFAULT_ENABLED=1\x00ROBOTHOR_SANDBOX_DEFAULT_MODE=observe\x00"
    )

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=dropin,
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_SANDBOX_DEFAULT_MODE")
    assert row.layer == "dropin"
    assert "OVERDUE" in row.tags
    assert "MISMATCH" not in row.tags


def test_debug_env_keys_appear_only_when_set(tmp_path):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_MODE=enforce\x00")

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    assert not [r for r in rows if "DEBUG-ENV" in r.tags]

    environ.write_bytes(b"ROBOTHOR_RBAC_MODE=enforce\x00ROBOTHOR_DISABLE_ALL_RIPS=1\x00")
    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    debug = [r for r in rows if "DEBUG-ENV" in r.tags]
    assert [r.flag for r in debug] == ["ROBOTHOR_DISABLE_ALL_RIPS"]
    assert debug[0].effective == "1"


def test_robothor_extra_path_is_a_debug_env_key(tmp_path):
    """ROBOTHOR_EXTRA_PATH is a root front-of-PATH lever documented as
    test-only (infra/systemd/README.md) — if a live process ever has it set,
    the daily audit must surface it under DEBUG-ENV like the other panic
    switches, not silently miss it because it isn't a governed flag."""
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_MODE=enforce\x00ROBOTHOR_EXTRA_PATH=/x\x00")

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    debug = [r for r in rows if "DEBUG-ENV" in r.tags]
    assert [r.flag for r in debug] == ["ROBOTHOR_EXTRA_PATH"]
    assert debug[0].effective == "/x"


def test_missing_environ_file_degrades_to_the_file_layers(tmp_path):
    """A watchdog that cannot read the running process must say so, not
    silently report the files as if they were live."""
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_RBAC_ENABLED=1\nROBOTHOR_RBAC_MODE=enforce\n")

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=tmp_path / "no-such-environ",
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")
    assert row.effective == "enforce"
    assert row.layer == "envfile"


# --- evidence columns -------------------------------------------------------


def test_rows_7d_renders_counts_by_action(tmp_path):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_RBAC_ENABLED=1\nROBOTHOR_RBAC_MODE=enforce\n")
    fired = dt.datetime(2026, 9, 1, 8, 30, tzinfo=dt.UTC)
    probed = dt.datetime(2026, 8, 30, 7, 0, tzinfo=dt.UTC)
    db = fa.DbState(
        pins={},
        evidence={
            "ROBOTHOR_RBAC_MODE": fa.Evidence(
                rows_7d={"blocked": 52, "observed": 7}, last_fired=fired, last_probe=probed
            )
        },
    )
    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=tmp_path / "none",
        db=db,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")
    assert row.rows_7d == "blocked=52 observed=7"
    assert row.last_fired == "2026-09-01 08:30"
    assert row.last_probe == "2026-08-30"


def test_evidence_columns_are_unknown_without_a_database(tmp_path):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_RBAC_MODE=enforce\n")
    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=tmp_path / "none",
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")
    assert row.rows_7d == "?"
    assert row.last_fired is None


# --- exit code and rendering ------------------------------------------------


def test_has_drift_only_for_mismatch_and_shadow_layer():
    def row(**kw):
        base = {
            "flag": "F",
            "yaml_mode": None,
            "dropin": None,
            "envfile": None,
            "db_pin": None,
            "effective": "observe",
            "layer": "code-default",
            "rows_7d": "?",
            "last_fired": None,
            "last_probe": None,
            "tags": (),
        }
        base.update(kw)
        return fa.FlagRow(**base)

    assert not fa.has_drift([row(tags=("OVERDUE",)), row(tags=("DEBUG-ENV",))])
    assert fa.has_drift([row(tags=("MISMATCH",))])
    assert fa.has_drift([row(tags=("SHADOW-LAYER:db",))])


def test_cli_exits_1_and_names_the_drifting_flag(tmp_path, capsys):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_ADMISSION_MODE", "mode": "observe"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_ADMISSION_ENABLED=1\nROBOTHOR_ADMISSION_MODE=enforce\n")

    rc = fa.main(
        [
            "--no-db",
            "--flags-yaml",
            str(manifest),
            "--env-file",
            str(env_file),
            "--dropin-dir",
            str(tmp_path / "none"),
            "--environ",
            str(tmp_path / "none"),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "ROBOTHOR_ADMISSION_MODE" in out
    assert "MISMATCH" in out
    # --no-db must announce the missing columns rather than print a blank one.
    assert "no database" in out.lower()


def test_cli_json_output_is_machine_readable(tmp_path, capsys):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_ADMISSION_MODE", "mode": "observe"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_ADMISSION_ENABLED=1\nROBOTHOR_ADMISSION_MODE=enforce\n")

    rc = fa.main(
        [
            "--no-db",
            "--json",
            "--flags-yaml",
            str(manifest),
            "--env-file",
            str(env_file),
            "--dropin-dir",
            str(tmp_path / "none"),
            "--environ",
            str(tmp_path / "none"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    row = next(r for r in payload["flags"] if r["flag"] == "ROBOTHOR_ADMISSION_MODE")
    assert row["effective"] == "enforce"
    assert row["layer"] == "envfile"
    assert "MISMATCH" in row["tags"]
    assert payload["drift"] is True


def test_cli_exits_0_when_the_versioned_dropin_alone_governs(tmp_path, capsys):
    """The clean state: the manifest's mode, set only in the drop-in that git
    tracks and check_dropin_drift.sh watches."""
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_ADMISSION_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    dropin = _dropin(tmp_path, ["ROBOTHOR_ADMISSION_ENABLED=1", "ROBOTHOR_ADMISSION_MODE=enforce"])

    rc = fa.main(
        [
            "--no-db",
            "--flags-yaml",
            str(manifest),
            "--env-file",
            str(env_file),
            "--dropin-dir",
            str(dropin),
            "--environ",
            str(tmp_path / "none"),
        ]
    )
    capsys.readouterr()
    assert rc == 0


def test_a_flag_living_only_in_the_env_file_is_a_shadow_layer(tmp_path):
    """The case check_dropin_drift.sh structurally cannot see.

    It compares the drop-in against its mirror and reports SHADOWED only for a
    name set in BOTH files. A guardrail that exists ONLY in the unversioned
    /etc/robothor/robothor.env passes that check silently: nothing in git says
    it is on, and a rebuilt box would come up without it.
    """
    manifest = _manifest(
        tmp_path, [{"name": "ROBOTHOR_COMPLETION_CONTRACTS_MODE", "mode": "enforce"}]
    )
    env_file = tmp_path / "robothor.env"
    env_file.write_text(
        "ROBOTHOR_COMPLETION_CONTRACTS_ENABLED=1\nROBOTHOR_COMPLETION_CONTRACTS_MODE=enforce\n"
    )
    environ = tmp_path / "environ"
    environ.write_bytes(
        b"ROBOTHOR_COMPLETION_CONTRACTS_ENABLED=1\x00ROBOTHOR_COMPLETION_CONTRACTS_MODE=enforce\x00"
    )

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_COMPLETION_CONTRACTS_MODE")
    # The manifest and the running process agree on `enforce` — this is not a
    # mismatch, it is unversioned posture, and only the tag says so.
    assert "MISMATCH" not in row.tags
    assert "SHADOW-LAYER:envfile" in row.tags
    assert fa.has_drift(rows)


def test_a_flag_set_outside_every_known_file_is_a_shadow_layer(tmp_path):
    """`systemctl set-environment`, the unit file, or the launching shell: the
    process carries a value no file on this box explains."""
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_ENABLED=1\x00ROBOTHOR_RBAC_MODE=enforce\x00")

    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=None,
        today=TODAY,
    )
    row = next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")
    assert row.layer == "environ"
    assert "SHADOW-LAYER:environ" in row.tags


# --- the audited universe ---------------------------------------------------


def test_every_governed_flag_is_audited(tmp_path):
    """A flag the controls dashboard can set but the audit never prints is a
    layer nobody checks."""
    from robothor.flags.store import GOVERNED_FLAGS

    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    rows = fa.audit(
        flags_yaml=REPO_ROOT / "infra" / "flags.yaml",
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=tmp_path / "none",
        db=None,
        today=TODAY,
    )
    audited = {r.flag for r in rows}
    assert audited >= GOVERNED_FLAGS


@pytest.mark.parametrize(
    "flag",
    [
        "ROBOTHOR_RUN_VERIFICATION_MODE",
        "ROBOTHOR_TOOL_VERIFY_MODE",
        "ROBOTHOR_BENCHMARK_DECONTAMINATION_MODE",
        "ROBOTHOR_DELIVERABLE_CONTRACT_MODE",
        "ROBOTHOR_HONESTY_SUITE_MODE",
        "ROBOTHOR_BENCHMARK_SANDBOX_MODE",
    ],
)
def test_newly_governed_flags_are_operator_settable_and_have_evidence(flag):
    """These six controls shipped with a rollout ladder but were unreachable
    from the controls API and had no evidence source — the dashboard could not
    show them and nothing could say whether they had ever fired."""
    from robothor.flags.evidence import EVIDENCE_SOURCES
    from robothor.flags.store import GOVERNED_FLAGS, valid_values_for

    assert flag in GOVERNED_FLAGS
    assert flag in EVIDENCE_SOURCES
    assert "observe" in valid_values_for(flag)


# --- guardrail_watch wiring -------------------------------------------------


def _guardrail_watch():
    spec = importlib.util.spec_from_file_location(
        "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_sibling_checks(monkeypatch, gw):
    """Default every check `main()` calls to a safe pass, matching each
    check's real signature, so a test driving `main()` for one check does not
    also run its siblings for real. `check_instance_doctor` hits the live
    box and `send_telegram` has real credentials on it — a test that forgets
    to stub either does not just fail loud, it pages the operator or shells
    out to instance_doctor.sh. Call this first, then override whichever
    check this test actually targets.
    """
    monkeypatch.setattr(gw, "check_flag_truth", lambda **kw: True)
    monkeypatch.setattr(gw, "check_instance_doctor", lambda script=None: True)
    monkeypatch.setattr(gw, "send_telegram", lambda text: False)


def test_check_flag_truth_runs_before_the_db_dependent_section(monkeypatch, capsys):
    """The truth table needs no database to produce its file-layer columns, so
    it belongs in main()'s DB-free half — the 2026-08-16 lesson: a DB outage
    must never take a DB-free check down with it.
    """
    gw = _guardrail_watch()
    order: list[str] = []

    _stub_sibling_checks(monkeypatch, gw)
    monkeypatch.setattr(gw, "check_soak_deadlines", lambda: order.append("soak"))
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: order.append("dropin"))
    monkeypatch.setattr(gw, "check_host_script_drift", lambda: order.append("host"))
    monkeypatch.setattr(
        gw, "check_instance_manifests", lambda: (order.append("manifests"), True)[1]
    )
    monkeypatch.setattr(gw, "check_flag_truth", lambda **kw: (order.append("flags"), True)[1])
    monkeypatch.setattr(gw, "check_slos", lambda: [])

    def _boom(*args, **kwargs) -> None:
        order.append("db")
        raise RuntimeError("postgres is not up yet")

    monkeypatch.setattr(gw, "_run_db_dependent_checks", _boom)

    rc = gw.main()
    capsys.readouterr()
    assert rc == 1
    assert order.index("flags") < order.index("db")


def test_main_exits_non_zero_when_the_flag_truth_table_drifts(monkeypatch, capsys):
    gw = _guardrail_watch()
    _stub_sibling_checks(monkeypatch, gw)
    for name in (
        "check_soak_deadlines",
        "check_dropin_drift",
        "check_host_script_drift",
        "_run_db_dependent_checks",
    ):
        monkeypatch.setattr(gw, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
    monkeypatch.setattr(gw, "check_flag_truth", lambda **kw: False)
    monkeypatch.setattr(gw, "check_slos", lambda: [])

    rc = gw.main()
    capsys.readouterr()
    assert rc == 1


# --- check_flag_truth: a crashed audit is not a disagreement ----------------


def _fake_subprocess_run(monkeypatch, gw, outcome, calls=None):
    """Replace the subprocess the watch spawns. No live audit, no live pager."""

    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((list(cmd), kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(gw.subprocess, "run", run)


def _capture_nags(monkeypatch, gw):
    """This box carries live Telegram credentials — nothing here may send."""
    sent: list[str] = []
    monkeypatch.setattr(gw, "send_telegram", lambda text: (sent.append(text), False)[1])
    return sent


def test_flag_audit_crash_is_reported_as_could_not_run_not_as_drift(monkeypatch, capsys):
    """rc=2 is the audit DYING — an import error, a missing yaml, a drifted
    schema. Paging "FLAG LAYERS DISAGREE" for it sends the operator to stare
    at flags that are fine, while the stderr saying what actually broke was
    captured and thrown away.
    """
    gw = _guardrail_watch()
    sent = _capture_nags(monkeypatch, gw)
    _fake_subprocess_run(
        monkeypatch,
        gw,
        subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="Traceback (most recent call last):\nModuleNotFoundError: No module named 'yaml'\n",
        ),
    )

    ok = gw.check_flag_truth()

    out = capsys.readouterr().out
    assert ok is False
    assert "could not run (rc=2)" in out
    assert "No module named 'yaml'" in out, "the captured stderr must be printed"
    assert "DISAGREE" not in out
    assert not any("DISAGREE" in text for text in sent)


def test_flag_audit_timeout_is_reported_as_could_not_run(monkeypatch, capsys):
    gw = _guardrail_watch()
    _capture_nags(monkeypatch, gw)
    _fake_subprocess_run(
        monkeypatch, gw, subprocess.TimeoutExpired(cmd="flag_audit.py", timeout=180)
    )

    ok = gw.check_flag_truth()

    out = capsys.readouterr().out
    assert ok is False
    assert "could not run" in out
    assert "DISAGREE" not in out


def test_flag_audit_rc_1_with_a_table_is_real_drift(monkeypatch, capsys):
    gw = _guardrail_watch()
    sent = _capture_nags(monkeypatch, gw)
    _fake_subprocess_run(
        monkeypatch,
        gw,
        subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="flag  yaml  effective\nROBOTHOR_RBAC_MODE  observe  enforce\nFAIL: a layer is shadowed\n",
            stderr="",
        ),
    )

    ok = gw.check_flag_truth()

    out = capsys.readouterr().out
    assert ok is False
    assert "DISAGREE" in out
    assert "could not run" not in out
    assert any("DISAGREE" in text for text in sent)


def test_flag_audit_rc_1_with_no_table_is_a_crash_not_drift(monkeypatch, capsys):
    """The audit exits 1 on drift *after* printing the table. Exit 1 with
    nothing on stdout is argparse or an early raise, not a verdict."""
    gw = _guardrail_watch()
    _capture_nags(monkeypatch, gw)
    _fake_subprocess_run(
        monkeypatch,
        gw,
        subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error: unrecognized arguments: --no-db\n"
        ),
    )

    ok = gw.check_flag_truth()

    out = capsys.readouterr().out
    assert ok is False
    assert "could not run (rc=1)" in out
    assert "unrecognized arguments" in out
    assert "DISAGREE" not in out


def test_flag_audit_rc_0_is_ok_and_pages_nobody(monkeypatch, capsys):
    gw = _guardrail_watch()
    sent = _capture_nags(monkeypatch, gw)
    _fake_subprocess_run(
        monkeypatch,
        gw,
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="flag  yaml\nOK — every layer agrees\n", stderr=""
        ),
    )

    ok = gw.check_flag_truth()

    out = capsys.readouterr().out
    assert ok is True
    assert "every layer agrees" in out
    assert sent == []


def test_check_flag_truth_passes_no_db_and_the_requested_timeout(monkeypatch, capsys):
    gw = _guardrail_watch()
    _capture_nags(monkeypatch, gw)
    calls: list[tuple[list[str], dict]] = []
    _fake_subprocess_run(
        monkeypatch,
        gw,
        subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr=""),
        calls=calls,
    )

    gw.check_flag_truth(no_db=True)
    gw.check_flag_truth(no_db=False, timeout=60)
    capsys.readouterr()

    assert "--no-db" in calls[0][0]
    assert "--no-db" not in calls[1][0]
    assert calls[1][1]["timeout"] == 60


def test_main_audits_the_file_layers_without_the_db_then_again_with_it(monkeypatch, capsys):
    """The DB-free half must not need postgres (2026-08-16), but the pin,
    actor and evidence columns exist only with it — so the watch runs both."""
    gw = _guardrail_watch()
    calls: list[dict] = []
    order: list[str] = []

    def fake_truth(**kwargs):
        calls.append(kwargs)
        order.append("flags")
        return True

    _stub_sibling_checks(monkeypatch, gw)
    monkeypatch.setattr(gw, "check_flag_truth", fake_truth)
    monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
    monkeypatch.setattr(gw, "check_host_script_drift", lambda: None)
    monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
    monkeypatch.setattr(gw, "check_slos", lambda: [])
    monkeypatch.setattr(gw, "_run_db_dependent_checks", lambda *args, **kwargs: order.append("db"))

    rc = gw.main()
    capsys.readouterr()

    assert rc == 0
    assert [c.get("no_db") for c in calls] == [True, False]
    assert calls[1].get("timeout") == 60
    assert order == ["flags", "db", "flags"]


def test_main_fails_when_only_the_db_pass_of_the_audit_finds_drift(monkeypatch, capsys):
    """A feature_flags pin is invisible to the --no-db pass; if the second
    pass is not allowed to fail the run, an unversioned DB pin never pages."""
    gw = _guardrail_watch()
    results = iter([True, False])
    _stub_sibling_checks(monkeypatch, gw)
    monkeypatch.setattr(gw, "check_flag_truth", lambda **kw: next(results))
    monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
    monkeypatch.setattr(gw, "check_host_script_drift", lambda: None)
    monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
    monkeypatch.setattr(gw, "check_slos", lambda: [])
    monkeypatch.setattr(gw, "_run_db_dependent_checks", lambda *args, **kwargs: None)

    rc = gw.main()
    capsys.readouterr()
    assert rc == 1


# --- a Controls-dashboard flip is not a shadow layer ------------------------


class _FakeCursor:
    """Queue-driven psycopg2 cursor, same shape as tests/test_operator_identity.py.

    Each ``execute`` pops the next ``{"fetchone": ..., "fetchall": [...]}``
    step and records the SQL, so a test can assert on the predicate text the
    audit actually sends — the read path has no other observable behaviour.
    """

    def __init__(self, script):
        self._script = list(script)
        self._fetchone = None
        self._fetchall = []
        self.executed: list[tuple[str, tuple]] = []
        self.connection = type("_Conn", (), {"rollback": lambda self: None})()

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params) if params else ()))
        step = self._script.pop(0) if self._script else {}
        self._fetchone = step.get("fetchone")
        self._fetchall = step.get("fetchall", [])

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **kw):
        return self._cursor

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_connection(monkeypatch, cursor):
    """fetch_db_state imports get_connection lazily — patch it at the source
    module, which is where the lazy import resolves it."""
    import robothor.db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **kw: _FakeConnection(cursor))


def _db_row(tmp_path, *, pin, actor, manifest_mode):
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": manifest_mode}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_ENABLED=1\x00")
    db = fa.DbState(
        pins={"ROBOTHOR_RBAC_MODE": pin},
        evidence={},
        pin_actors={"ROBOTHOR_RBAC_MODE": actor},
    )
    rows = fa.audit(
        flags_yaml=manifest,
        env_file=env_file,
        dropin_dir=tmp_path / "none",
        environ_path=environ,
        db=db,
        today=TODAY,
    )
    return rows, next(r for r in rows if r.flag == "ROBOTHOR_RBAC_MODE")


def test_an_operator_written_pin_is_informational_not_drift(tmp_path):
    """The Controls dashboard writes `feature_flags` through store.set_flag,
    stamped `operator:<actor_id>` by routers/_operator.require_operator. That
    IS the supported way to flip a guardrail; tagging it SHADOW-LAYER made
    every legitimate flip page the operator every morning, forever.
    """
    rows, row = _db_row(tmp_path, pin="observe", actor="operator:u-123", manifest_mode="observe")
    assert row.layer == "db"
    assert "PINNED:db@operator:u-123" in row.tags
    assert not any(t.startswith("SHADOW-LAYER") for t in row.tags)
    assert not fa.has_drift(rows), "a dashboard flip must not exit 1"


def test_a_pin_from_an_unknown_actor_is_still_a_shadow_layer(tmp_path):
    """A row nothing operator-facing wrote — a sync job, a script, a stray
    psql — is exactly the unversioned posture this audit exists to name."""
    rows, row = _db_row(
        tmp_path, pin="observe", actor="engine-posture-sync", manifest_mode="observe"
    )
    assert "SHADOW-LAYER:db" in row.tags
    assert not any(t.startswith("PINNED") for t in row.tags)
    assert fa.has_drift(rows)


def test_the_migration_seed_row_is_not_a_pin_at_all(monkeypatch):
    """migration-084's seed row means "unset" (robothor/flags/store._read_db),
    so it must never reach the table as a pin of any class."""
    from robothor.flags.store import _SEED_ACTOR

    cursor = _FakeCursor(
        [
            {
                "fetchall": [
                    ("ROBOTHOR_RBAC_MODE", "observe", _SEED_ACTOR),
                    ("ROBOTHOR_JUDGE_ENABLED", "true", "operator:u-123"),
                ]
            }
        ]
    )
    _patch_connection(monkeypatch, cursor)

    state = fa.fetch_db_state(["ROBOTHOR_RBAC_MODE", "ROBOTHOR_JUDGE_ENABLED"])

    assert "ROBOTHOR_RBAC_MODE" not in state.pins
    assert state.pins["ROBOTHOR_JUDGE_ENABLED"] == "true"
    assert state.pin_actors["ROBOTHOR_JUDGE_ENABLED"] == "operator:u-123"


def test_an_operator_pin_that_contradicts_the_manifest_still_fails(tmp_path):
    """PINNED downgrades the shadow tag only. A dashboard flip the manifest
    does not record is still a lie in infra/flags.yaml."""
    rows, row = _db_row(tmp_path, pin="enforce", actor="operator:u-123", manifest_mode="observe")
    assert "PINNED:db@operator:u-123" in row.tags
    assert "MISMATCH" in row.tags
    assert fa.has_drift(rows)


# --- an invalid value is clamped, exactly as the reader clamps it -----------


def test_effective_value_clamps_an_out_of_range_mode_like_the_reader_does():
    """ROBOTHOR_RIP_13_MODE only honors observe/enforce
    (feature_flags.symbolic_memory_mode drops anything else and returns
    observe). Printing `alert` as the effective value describes a system that
    does not exist — the exact class of defect this audit exists to find.
    """
    gates = fa.mode_gate_map()
    notes: list[str] = []
    resolved = {"ROBOTHOR_RIP_13_ENABLED": "1", "ROBOTHOR_RIP_13_MODE": "alert"}

    assert fa.effective_value("ROBOTHOR_RIP_13_MODE", resolved, gates, notes=notes) == "observe"
    assert any("ROBOTHOR_RIP_13_MODE" in n and "alert" in n for n in notes), (
        "the raw value must be printed — a silently clamped flag is a flag "
        "whose /etc line nobody knows is dead"
    )


def test_effective_value_leaves_a_valid_value_alone_and_notes_nothing():
    gates = fa.mode_gate_map()
    notes: list[str] = []
    resolved = {"ROBOTHOR_RIP_13_ENABLED": "1", "ROBOTHOR_RIP_13_MODE": "enforce"}
    assert fa.effective_value("ROBOTHOR_RIP_13_MODE", resolved, gates, notes=notes) == "enforce"
    assert notes == []


def test_cli_notes_the_raw_value_of_a_clamped_flag(tmp_path, capsys):
    """Set only in the versioned drop-in, so nothing else in the table can
    fail the run: the clamp alone decides the exit code here."""
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RIP_13_MODE", "mode": "observe"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    dropin = _dropin(tmp_path, ["ROBOTHOR_RIP_13_ENABLED=1", "ROBOTHOR_RIP_13_MODE=alert"])

    rc = fa.main(
        [
            "--no-db",
            "--flags-yaml",
            str(manifest),
            "--env-file",
            str(env_file),
            "--dropin-dir",
            str(dropin),
            "--environ",
            str(tmp_path / "none"),
        ]
    )
    out = capsys.readouterr().out

    assert "alert" in out
    assert "not one of observe, enforce" in out, "the note must name the valid set"
    # Clamped to observe, which is what the manifest records — so the run does
    # not fail on a MISMATCH the engine never had.
    row_line = next(ln for ln in out.splitlines() if ln.startswith("ROBOTHOR_RIP_13_MODE"))
    assert "observe" in row_line
    assert rc == 0


# --- systemd's real Environment= syntax -------------------------------------


def test_parse_dropin_dir_reads_several_assignments_on_one_line(tmp_path):
    """`Environment=A=1 B=2` is valid systemd and sets BOTH. Partitioning on
    the first `=` read it as one flag whose value was `1 B=2`, so the second
    flag was invisible to the audit and the first had a value the engine never
    saw."""
    d = tmp_path / "dropin"
    d.mkdir()
    (d / "a.conf").write_text(
        "[Service]\nEnvironment=ROBOTHOR_RBAC_ENABLED=1 ROBOTHOR_RBAC_MODE=enforce\n"
    )
    assert fa.parse_dropin_dir(d) == {
        "ROBOTHOR_RBAC_ENABLED": "1",
        "ROBOTHOR_RBAC_MODE": "enforce",
    }


def test_parse_dropin_dir_reads_quoted_assignments(tmp_path):
    """systemd's own quoted form, and a value containing a space."""
    d = tmp_path / "dropin"
    d.mkdir()
    (d / "a.conf").write_text(
        "[Service]\n"
        'Environment="ROBOTHOR_RBAC_ENABLED=1" "ROBOTHOR_RBAC_MODE=enforce"\n'
        'Environment="ROBOTHOR_NOTE=two words"\n'
    )
    assert fa.parse_dropin_dir(d) == {
        "ROBOTHOR_RBAC_ENABLED": "1",
        "ROBOTHOR_RBAC_MODE": "enforce",
        "ROBOTHOR_NOTE": "two words",
    }


def test_parse_dropin_dir_survives_an_unbalanced_quote(tmp_path):
    """A hand-edited drop-in must not take the whole audit down."""
    d = tmp_path / "dropin"
    d.mkdir()
    (d / "a.conf").write_text('[Service]\nEnvironment=ROBOTHOR_RBAC_MODE="enforce\n')
    assert fa.parse_dropin_dir(d) == {"ROBOTHOR_RBAC_MODE": "enforce"}


# --- the running process is read once ---------------------------------------


def test_main_reads_the_running_environ_exactly_once(tmp_path, monkeypatch, capsys):
    """main() read /proc/<MainPID>/environ to decide whether to print the
    "could not read the running engine" note, and audit() then read it again
    to build the table. Two reads of the same file can disagree — the engine
    can restart between them — and the report would then mix a note about one
    process with a table about another.
    """
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("")
    environ = tmp_path / "environ"
    environ.write_bytes(b"ROBOTHOR_RBAC_ENABLED=1\x00ROBOTHOR_RBAC_MODE=enforce\x00")

    seen: list = []
    real = fa.read_environ

    def counting(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(fa, "read_environ", counting)
    fa.main(
        [
            "--no-db",
            "--flags-yaml",
            str(manifest),
            "--env-file",
            str(env_file),
            "--dropin-dir",
            str(tmp_path / "none"),
            "--environ",
            str(environ),
        ]
    )
    capsys.readouterr()
    assert len(seen) == 1, f"read {len(seen)} times: {seen}"


def test_main_still_notes_an_unreadable_environ(tmp_path, capsys):
    """The single read must keep the degradation notice — the note is the
    only thing separating a simulated table from a measured one."""
    manifest = _manifest(tmp_path, [{"name": "ROBOTHOR_RBAC_MODE", "mode": "enforce"}])
    env_file = tmp_path / "robothor.env"
    env_file.write_text("ROBOTHOR_RBAC_ENABLED=1\nROBOTHOR_RBAC_MODE=enforce\n")

    fa.main(
        [
            "--no-db",
            "--flags-yaml",
            str(manifest),
            "--env-file",
            str(env_file),
            "--dropin-dir",
            str(tmp_path / "none"),
            "--environ",
            str(tmp_path / "no-such-environ"),
        ]
    )
    out = capsys.readouterr().out
    assert "could not read the running engine's environment" in out
    assert "simulated" in out


# --- the DB read path -------------------------------------------------------
#
# Three functions that only ever ran against the live database. Their whole
# behaviour is the SQL they send — a wrong predicate returns a comforting zero
# and makes this detector a liar (robothor/flags/evidence.py says exactly
# that) — so these assert the predicate text, on the same injected cursor the
# rest of the suite uses.


def test_fetch_db_state_asks_only_for_the_flags_it_audits(monkeypatch):
    from robothor.flags.evidence import EVIDENCE_SOURCES

    flag = "ROBOTHOR_RBAC_MODE"
    fired = dt.datetime(2026, 9, 1, 8, 30, tzinfo=dt.UTC)
    probed = dt.datetime(2026, 8, 30, 7, 0, tzinfo=dt.UTC)
    cursor = _FakeCursor(
        [
            {"fetchall": [(flag, "observe", "operator:u-1")]},
            {"fetchone": (f"public.{EVIDENCE_SOURCES[flag].table}",)},
            {"fetchall": [("observed", 3), ("blocked", 1)]},
            {"fetchone": (fired,)},
            {"fetchone": (probed,)},
        ]
    )
    _patch_connection(monkeypatch, cursor)

    state = fa.fetch_db_state([flag])

    pin_sql, pin_params = cursor.executed[0]
    assert "FROM feature_flags WHERE name = ANY(%s)" in pin_sql
    assert pin_params == ([flag],), "one query for the audited set, not one per flag"
    assert state.pins == {flag: "observe"}
    assert state.evidence[flag].rows_7d == {"observed": 3, "blocked": 1}


def test_fetch_db_state_skips_flags_with_no_declared_evidence_source(monkeypatch):
    """A flag with no EvidenceSource must send NO evidence query at all —
    querying the wrong table for it would report a zero that means nothing."""
    from robothor.flags.evidence import EVIDENCE_SOURCES

    unsourced = next(f for f in fa.DEBUG_ENV_KEYS if f not in EVIDENCE_SOURCES)
    cursor = _FakeCursor([{"fetchall": []}])
    _patch_connection(monkeypatch, cursor)

    state = fa.fetch_db_state([unsourced])

    assert len(cursor.executed) == 1, "only the feature_flags read"
    assert state.evidence == {}


def test_fetch_one_evidence_groups_the_event_log_by_action(monkeypatch):
    """`observed` (would have blocked) vs `blocked` is the promotion decision;
    a bare count cannot make it."""
    from robothor.flags.evidence import EVIDENCE_SOURCES

    src = EVIDENCE_SOURCES["ROBOTHOR_RBAC_MODE"]
    fired = dt.datetime(2026, 9, 1, 8, 30, tzinfo=dt.UTC)
    cursor = _FakeCursor(
        [
            {"fetchone": ("public.agent_guardrail_events",)},
            {"fetchall": [("observed", 7)]},
            {"fetchone": (fired,)},
            {"fetchone": (None,)},
        ]
    )

    ev = fa._fetch_one_evidence(cursor, "ROBOTHOR_RBAC_MODE", src)

    guard_sql, guard_params = cursor.executed[0]
    assert "to_regclass" in guard_sql
    assert guard_params == ("public.agent_guardrail_events",)
    counts_sql = cursor.executed[1][0]
    assert "GROUP BY action" in counts_sql
    assert src.where in counts_sql
    assert "interval '7 days'" in counts_sql
    assert ev.rows_7d == {"observed": 7}
    assert ev.last_fired == fired


def test_fetch_one_evidence_reports_a_missing_table_instead_of_a_zero(monkeypatch):
    """Table presence is deploy-specific. "no <table>" and "0 rows" are
    different findings and must not be printed as the same one."""
    from robothor.flags.evidence import EVIDENCE_SOURCES

    src = EVIDENCE_SOURCES["ROBOTHOR_JUDGE_ENABLED"]
    cursor = _FakeCursor([{"fetchone": (None,)}])

    ev = fa._fetch_one_evidence(cursor, "ROBOTHOR_JUDGE_ENABLED", src)

    assert ev.note == f"no {src.table}"
    assert ev.rows_7d == {}
    assert len(cursor.executed) == 1, "a missing table must not be queried"


def test_fetch_one_evidence_counts_rows_for_a_non_event_table(monkeypatch):
    """Only agent_guardrail_events has an `action` column; every other source
    is counted on its own time column."""
    from robothor.flags.evidence import EVIDENCE_SOURCES

    src = EVIDENCE_SOURCES["ROBOTHOR_JUDGE_ENABLED"]
    fired = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.UTC)
    cursor = _FakeCursor(
        [
            {"fetchone": (f"public.{src.table}",)},
            {"fetchone": (fired, 4)},
            {"fetchone": (None,)},
        ]
    )

    ev = fa._fetch_one_evidence(cursor, "ROBOTHOR_JUDGE_ENABLED", src)

    counts_sql = cursor.executed[1][0]
    assert "GROUP BY action" not in counts_sql
    assert src.time_column in counts_sql
    assert src.where in counts_sql
    assert ev.rows_7d == {"rows": 4}
    assert ev.last_fired == fired


def test_fetch_one_evidence_rolls_back_and_notes_a_drifted_schema(monkeypatch):
    """A dropped column must degrade this one flag's evidence, not abort the
    audit — and the aborted transaction must be rolled back or every query
    after it fails too."""
    from robothor.flags.evidence import EVIDENCE_SOURCES

    src = EVIDENCE_SOURCES["ROBOTHOR_RBAC_MODE"]
    rolled: list[bool] = []

    class _Exploding(_FakeCursor):
        def execute(self, sql, params=()):
            self.executed.append((sql, tuple(params) if params else ()))
            if len(self.executed) == 1:
                self._fetchone = ("public.agent_guardrail_events",)
                return
            raise RuntimeError('column "action" does not exist')

    cursor = _Exploding([])
    cursor.connection = type("_C", (), {"rollback": lambda self: rolled.append(True)})()

    ev = fa._fetch_one_evidence(cursor, "ROBOTHOR_RBAC_MODE", src)

    assert ev.note == f"query failed on {src.table}"
    assert rolled == [True]


def test_fetch_last_probe_matches_the_probe_trigger_detail():
    """A probe run is the only evidence separating "nothing violated this"
    from "this control cannot fire" — it is matched on the trigger_detail
    prefix the probe runs stamp themselves with."""
    probed = dt.datetime(2026, 8, 30, 7, 0, tzinfo=dt.UTC)
    cursor = _FakeCursor([{"fetchone": (probed,)}])

    got = fa._fetch_last_probe(cursor, "ROBOTHOR_RBAC_MODE")

    sql, params = cursor.executed[0]
    assert "max(started_at)" in sql
    assert "FROM agent_runs WHERE trigger_detail LIKE %s" in sql
    assert params == ("probe:ROBOTHOR_RBAC_MODE%",)
    assert got == probed


def test_fetch_last_probe_survives_a_missing_agent_runs_table():
    rolled: list[bool] = []

    class _Exploding(_FakeCursor):
        def execute(self, sql, params=()):
            self.executed.append((sql, tuple(params) if params else ()))
            raise RuntimeError("relation agent_runs does not exist")

    cursor = _Exploding([])
    cursor.connection = type("_C", (), {"rollback": lambda self: rolled.append(True)})()

    assert fa._fetch_last_probe(cursor, "ROBOTHOR_RBAC_MODE") is None
    assert rolled == [True]
