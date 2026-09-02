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


def test_check_flag_truth_runs_before_the_db_dependent_section(monkeypatch, capsys):
    """The truth table needs no database to produce its file-layer columns, so
    it belongs in main()'s DB-free half — the 2026-08-16 lesson: a DB outage
    must never take a DB-free check down with it.
    """
    gw = _guardrail_watch()
    order: list[str] = []

    monkeypatch.setattr(gw, "check_soak_deadlines", lambda: order.append("soak"))
    monkeypatch.setattr(gw, "check_dropin_drift", lambda: order.append("dropin"))
    monkeypatch.setattr(gw, "check_host_script_drift", lambda: order.append("host"))
    monkeypatch.setattr(
        gw, "check_instance_manifests", lambda: (order.append("manifests"), True)[1]
    )
    monkeypatch.setattr(gw, "check_flag_truth", lambda: (order.append("flags"), True)[1])

    def _boom() -> None:
        order.append("db")
        raise RuntimeError("postgres is not up yet")

    monkeypatch.setattr(gw, "_run_db_dependent_checks", _boom)

    rc = gw.main()
    capsys.readouterr()
    assert rc == 1
    assert order.index("flags") < order.index("db")


def test_main_exits_non_zero_when_the_flag_truth_table_drifts(monkeypatch, capsys):
    gw = _guardrail_watch()
    for name in (
        "check_soak_deadlines",
        "check_dropin_drift",
        "check_host_script_drift",
        "_run_db_dependent_checks",
    ):
        monkeypatch.setattr(gw, name, lambda: None)
    monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
    monkeypatch.setattr(gw, "check_flag_truth", lambda: False)

    rc = gw.main()
    capsys.readouterr()
    assert rc == 1
