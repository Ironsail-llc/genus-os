"""Which systemd file sets a setting, and changing it there.

``genus config get`` used to say only ``source: env`` for a value a systemd
drop-in injected, so an agent blocked by one could not name the file, and had
no supported way to change it. These tests pin the pure parsing (file contents
in, origin out) and the apply path with every subprocess call injected -- no
test here reads /etc or runs systemctl.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest

from robothor.settings import systemd_env as se

UNIT = "robothor-engine.service"
FRAGMENT = "/etc/systemd/system/robothor-engine.service"
DROPDIR = "/etc/systemd/system/robothor-engine.service.d"


# ── parsing ──────────────────────────────────────────────────────────────────


def test_environment_line_parses_quoted_and_multiple_assignments() -> None:
    assert se.parse_environment_value("\"A=one two\" B=2 'C=x'") == [
        ("A", "one two"),
        ("B", "2"),
        ("C", "x"),
    ]


def test_env_file_parsing_skips_comments_and_strips_quotes() -> None:
    text = "# comment\n; also\nA=1\nB=\"two words\"\n\nC='3'\nnot an assignment\n"
    assert se.parse_env_file(text) == {"A": "1", "B": "two words", "C": "3"}


def test_last_dropin_setting_the_variable_wins() -> None:
    files = [
        (FRAGMENT, "[Service]\nEnvironment=OTHER=1\n"),
        (f"{DROPDIR}/zz-cal.conf", "[Service]\nEnvironment=ROBOTHOR_X=true\n"),
        (f"{DROPDIR}/zzzz-cal-off.conf", "[Service]\nEnvironment=ROBOTHOR_X=false\n"),
    ]
    origin = se.find_origin("ROBOTHOR_X", files, read_env_file=lambda _p: None)
    assert origin is not None
    assert origin.path == f"{DROPDIR}/zzzz-cal-off.conf"
    assert origin.kind == "Environment"
    assert origin.value == "false"
    assert origin.line == 2
    # The one it beat is still reported, so the conflict is visible.
    assert [o.path for o in origin.shadowed] == [f"{DROPDIR}/zz-cal.conf"]


def test_environment_outside_the_service_section_is_ignored() -> None:
    files = [(FRAGMENT, "[Unit]\nEnvironment=ROBOTHOR_X=1\n[Install]\n")]
    assert se.find_origin("ROBOTHOR_X", files, read_env_file=lambda _p: None) is None


def test_empty_environment_resets_earlier_assignments() -> None:
    files = [
        (FRAGMENT, "[Service]\nEnvironment=ROBOTHOR_X=1\n"),
        (f"{DROPDIR}/reset.conf", "[Service]\nEnvironment=\n"),
    ]
    assert se.find_origin("ROBOTHOR_X", files, read_env_file=lambda _p: None) is None


def test_environment_file_beats_environment_and_reports_only_the_path() -> None:
    files = [
        (
            FRAGMENT,
            "[Service]\nEnvironmentFile=-/etc/robothor/robothor.env\n"
            "Environment=ROBOTHOR_X=from-unit\n",
        )
    ]
    origin = se.find_origin(
        "ROBOTHOR_X",
        files,
        read_env_file=lambda p: "ROBOTHOR_X=from-file\n" if p.endswith("robothor.env") else None,
    )
    assert origin is not None
    assert origin.kind == "EnvironmentFile"
    assert origin.path == "/etc/robothor/robothor.env"
    assert origin.declared_in == FRAGMENT


def test_unreadable_environment_file_is_noted_not_guessed() -> None:
    files = [(FRAGMENT, "[Service]\nEnvironmentFile=-/run/robothor/secrets.env\n")]
    unreadable: list[str] = []
    origin = se.find_origin(
        "ROBOTHOR_X", files, read_env_file=lambda _p: None, unreadable=unreadable
    )
    assert origin is None
    assert unreadable == ["/run/robothor/secrets.env"]


def test_show_output_parses_fragment_and_dropins() -> None:
    out = f"FragmentPath={FRAGMENT}\nDropInPaths={DROPDIR}/a.conf {DROPDIR}/b.conf\n"
    assert se.parse_show(out) == [FRAGMENT, f"{DROPDIR}/a.conf", f"{DROPDIR}/b.conf"]


# ── describing the origin without leaking a credential ───────────────────────


def test_describe_masks_a_secret_looking_value() -> None:
    origin = se.Origin(
        unit=UNIT,
        name="SOME_API_KEY",
        kind="Environment",
        path="/x.conf",
        line=3,
        value="sk-live-abcdef1234567890",
    )
    text = se.describe(origin, secret=False)
    assert "sk-live-abcdef1234567890" not in text
    assert "/x.conf:3" in text


def test_describe_masks_a_declared_secret() -> None:
    origin = se.Origin(
        unit=UNIT,
        name="ROBOTHOR_PLAIN",
        kind="Environment",
        path="/x.conf",
        line=1,
        value="hunter2",
    )
    assert "hunter2" not in se.describe(origin, secret=True)


def test_describe_shows_a_plain_value() -> None:
    origin = se.Origin(
        unit=UNIT,
        name="ROBOTHOR_X",
        kind="Environment",
        path="/x.conf",
        line=1,
        value="false",
    )
    assert "false" in se.describe(origin, secret=False)


def test_describe_an_environment_file_never_shows_a_value() -> None:
    origin = se.Origin(
        unit=UNIT,
        name="ROBOTHOR_X",
        kind="EnvironmentFile",
        path="/etc/r.env",
        value="whatever",
        declared_in=FRAGMENT,
    )
    text = se.describe(origin, secret=False)
    assert "whatever" not in text
    assert "/etc/r.env" in text


# ── planning a drop-in ───────────────────────────────────────────────────────


def _origin(path: str, kind: str = "Environment") -> se.Origin:
    return se.Origin(unit=UNIT, name="ROBOTHOR_X", kind=kind, path=path, line=2, value="v")


def test_dropin_name_is_derived_from_the_key() -> None:
    plan = se.plan_dropin("ROBOTHOR_X", "true", UNIT, _origin(f"{DROPDIR}/40-a.conf"))
    assert plan.path == f"{DROPDIR}/zz-genus-config-robothor-x.conf"
    assert 'Environment="ROBOTHOR_X=true"' in plan.content


def test_dropin_escapes_systemd_specials() -> None:
    plan = se.plan_dropin("ROBOTHOR_X", 'a"b\\c%d', UNIT, None)
    assert 'Environment="ROBOTHOR_X=a\\"b\\\\c%%d"' in plan.content


def test_a_later_dropin_is_a_named_conflict() -> None:
    later = f"{DROPDIR}/zzzzzzzz-calendar-operations-off.conf"
    with pytest.raises(se.DropinConflictError) as exc:
        se.plan_dropin("ROBOTHOR_X", "true", UNIT, _origin(later))
    assert later in str(exc.value)
    assert "--override" in str(exc.value)


def test_override_writes_a_name_that_sorts_after_the_conflict() -> None:
    later = f"{DROPDIR}/zzzzzzzz-calendar-operations-off.conf"
    plan = se.plan_dropin("ROBOTHOR_X", "true", UNIT, _origin(later), override=True)
    assert plan.path.rsplit("/", 1)[1] > later.rsplit("/", 1)[1]
    assert plan.path.startswith(DROPDIR + "/")
    assert "robothor-x" in plan.path


def test_our_own_dropin_winning_is_not_a_conflict() -> None:
    own = f"{DROPDIR}/zz-genus-config-robothor-x.conf"
    plan = se.plan_dropin("ROBOTHOR_X", "true", UNIT, _origin(own))
    assert plan.path == own


def test_a_previous_override_dropin_is_reused() -> None:
    own = f"{DROPDIR}/zzzzzzzzz-genus-config-robothor-x.conf"
    plan = se.plan_dropin("ROBOTHOR_X", "true", UNIT, _origin(own))
    assert plan.path == own


def test_an_environment_file_cannot_be_overridden_by_a_dropin() -> None:
    origin = se.Origin(
        unit=UNIT,
        name="ROBOTHOR_X",
        kind="EnvironmentFile",
        path="/etc/robothor/robothor.env",
        declared_in=FRAGMENT,
    )
    with pytest.raises(se.DropinConflictError) as exc:
        se.plan_dropin("ROBOTHOR_X", "true", UNIT, origin, override=True)
    assert "/etc/robothor/robothor.env" in str(exc.value)


# ── applying ─────────────────────────────────────────────────────────────────


@dataclass
class Recorder:
    calls: list[list[str]] = field(default_factory=list)
    fail_on: str | None = None

    def __call__(self, argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        code = 1 if self.fail_on and self.fail_on in argv else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="denied" if code else "")


def test_apply_installs_reloads_and_schedules_a_delayed_restart(tmp_path) -> None:
    plan = se.plan_dropin("ROBOTHOR_X", "true", UNIT, None)
    run = Recorder()
    result = se.apply_dropins([plan], runner=run, scratch=tmp_path, stamp="20261006T120000")
    flat = [" ".join(c) for c in run.calls]
    assert any(c.startswith("sudo -n install") and plan.path in c for c in flat)
    assert "sudo -n systemctl daemon-reload" in flat
    restart = [c for c in run.calls if "systemd-run" in c]
    assert restart, flat
    assert "--on-active=15s" in restart[0]
    assert "--unit=genus-config-restart-20261006T120000" in restart[0]
    assert restart[0][-3:] == ["systemctl", "restart", UNIT]
    # install < daemon-reload < restart
    order = [flat.index(c) for c in flat]
    assert order == sorted(order)
    assert result.restart_unit == "genus-config-restart-20261006T120000"
    assert result.written == [plan.path]


def test_apply_stops_at_the_first_failure_and_says_which(tmp_path) -> None:
    plan = se.plan_dropin("ROBOTHOR_X", "true", UNIT, None)
    run = Recorder(fail_on="install")
    with pytest.raises(se.ApplyError) as exc:
        se.apply_dropins([plan], runner=run, scratch=tmp_path, stamp="t")
    assert "install" in str(exc.value)
    assert not any("daemon-reload" in c for c in run.calls)


def test_lookup_uses_injected_systemctl_and_reader() -> None:
    show = f"FragmentPath={FRAGMENT}\nDropInPaths={DROPDIR}/zz-a.conf\n"
    contents = {
        FRAGMENT: "[Service]\nEnvironmentFile=-/etc/robothor/robothor.env\n",
        f"{DROPDIR}/zz-a.conf": "[Service]\nEnvironment=ROBOTHOR_X=false\n",
        "/etc/robothor/robothor.env": "OTHER=1\n",
    }

    def run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        assert argv[:2] == ["systemctl", "show"]
        return subprocess.CompletedProcess(argv, 0, stdout=show, stderr="")

    origins = se.lookup("ROBOTHOR_X", ["robothor-engine"], runner=run, reader=contents.get)
    assert len(origins) == 1
    assert origins[0].unit == UNIT
    assert origins[0].path == f"{DROPDIR}/zz-a.conf"


def test_lookup_without_systemctl_is_empty() -> None:
    def run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        raise FileNotFoundError("systemctl")

    assert se.lookup("ROBOTHOR_X", ["robothor-engine"], runner=run, reader=lambda _p: None) == []
