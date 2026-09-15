"""``GET /api/logs`` — journald, for an operator who is not on the box.

No test in this module runs ``journalctl``. The subprocess is faked at the
router's own seam, which is the only way to assert the things that matter: what
argv is built, what happens when the binary is missing, and what happens when
it hangs. A test that shelled out would assert whatever this developer's
machine happened to have in its journal.

The claims:

* **The allowlist is what is installed, not a list somebody typed.** Derived
  from the unit files ``scripts/install-units.sh`` writes, with the repo's
  ``infra/systemd`` templates as the fallback. A unit not in it is a 422 that
  names the set — never a 500, and never an argv the caller chose.
* **argv is a list, and ``since`` cannot become a flag.** Validated against a
  strict pattern and then passed as ONE ``--since=<value>`` token, so there is
  no argv position a caller can reach into.
* **``grep`` never reaches journalctl.** It is applied in Python, to the
  REDACTED message — otherwise it is an oracle: ``?grep=sk-or-abc`` would
  confirm a secret's presence in a line whose output is redacted.
* **A missing journald is ``available: false`` with a sentence.** That is the
  container case, and a 500 there would read as a broken appliance.
* **A secret in a log line does not reach the browser.**
"""

from __future__ import annotations

import json
import subprocess

import pytest

LOGS = "/api/logs"
UNITS = "/api/logs/units"

#: A real OPENROUTER key shape with a visibly fake body. The value must not
#: survive the round trip.
SECRET_LINE = "starting engine with OPENROUTER_API_KEY=sk-or-abc123def456ghi789"


def _journal(*messages: str) -> str:
    """journalctl ``-o json`` output: one JSON object per line."""
    return "".join(
        json.dumps(
            {
                "__REALTIME_TIMESTAMP": str(1757000000000000 + i),
                "PRIORITY": "6",
                "MESSAGE": message,
            }
        )
        + "\n"
        for i, message in enumerate(messages)
    )


@pytest.fixture
def journal(monkeypatch):
    """A fake ``journalctl``: records argv, answers with canned output."""
    from routers import logs

    calls: list[list[str]] = []
    state = {"stdout": _journal("engine started"), "returncode": 0, "raises": None}

    def _run(argv, **kwargs):
        calls.append(list(argv))
        if state["raises"] is not None:
            raise state["raises"]
        return subprocess.CompletedProcess(argv, state["returncode"], state["stdout"], "")

    monkeypatch.setattr(logs, "_run", _run)
    monkeypatch.setattr(logs, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(logs, "unit_catalog", lambda: {"robothor-engine": "Genus OS Agent Engine"})
    return type("Journal", (), {"calls": calls, "state": state})()


# ── the gate ─────────────────────────────────────────────────────────────────


def test_a_service_token_may_not_read_the_journal(controls_client_as_service, journal):
    """An agent must not read the log of what the appliance did about it."""
    assert controls_client_as_service.get(UNITS).status_code == 403
    assert controls_client_as_service.get(f"{LOGS}?unit=robothor-engine").status_code == 403


def test_an_auditor_may_not_read_the_journal(controls_client_as_auditor, journal):
    """The journal is not the audit log: it carries every value every process
    printed, which is a wider surface than "what was done, by whom"."""
    assert controls_client_as_auditor.get(UNITS).status_code == 403
    assert controls_client_as_auditor.get(f"{LOGS}?unit=robothor-engine").status_code == 403


def test_a_member_may_not_read_the_journal(controls_client_as_user, journal):
    assert controls_client_as_user.get(f"{LOGS}?unit=robothor-engine").status_code == 403


def test_another_tenants_owner_may_not(controls_client_as_other_tenant_owner, journal):
    assert controls_client_as_other_tenant_owner.get(UNITS).status_code == 403


# ── the allowlist ────────────────────────────────────────────────────────────


def test_the_allowlist_is_the_set_of_installed_units():
    """Not a list in this file: the catalog reads the unit files that
    ``scripts/install-units.sh`` renders and installs."""
    from routers import logs

    catalog = logs.unit_catalog()
    assert "robothor-engine" in catalog
    assert "robothor-bridge" in catalog
    assert catalog["robothor-engine"], "each unit carries its Description="
    assert not any("@" in name for name in catalog), "template units cannot be followed"


def test_the_template_fallback_matches_what_the_installer_installs():
    """The guard against the catalog drifting from the installer.

    ``install-units.sh`` globs ``infra/systemd/robothor-*.service``; so does
    the fallback. If someone adds a unit template, it appears here for free,
    and if the glob in either place changes, this fails.
    """
    from routers import logs

    from_templates = {
        path.stem
        for path in logs.TEMPLATE_UNIT_DIR.glob("robothor-*.service")
        if "@" not in path.stem
    }
    assert from_templates, "infra/systemd is tracked; an empty set means the path is wrong"
    assert from_templates == set(logs._catalog_from(logs.TEMPLATE_UNIT_DIR))
    assert {"robothor-engine", "robothor-bridge", "robothor-app"} <= from_templates


def test_the_catalog_is_not_rebuilt_on_every_request(monkeypatch):
    """One directory listing plus a file read per unit, per request, is a cost
    the page does not need to pay — the installed unit set changes when
    somebody runs the installer, not between two clicks."""
    from routers import logs

    logs.reset_unit_catalog_cache()
    calls: list = []
    real = logs._catalog_from

    def _counted(directory):
        calls.append(directory)
        return real(directory)

    monkeypatch.setattr(logs, "_catalog_from", _counted)
    try:
        first = logs.unit_catalog()
        assert calls, "the first call must actually read the units"
        before = len(calls)
        assert logs.unit_catalog() == first
        assert len(calls) == before, "the second call inside the TTL re-read the filesystem"
    finally:
        logs.reset_unit_catalog_cache()


def test_reading_a_unit_does_not_spawn_a_second_process(controls_client_as_operator, journal):
    """``journalctl --version`` before every read was a probe answering a
    question the read itself answers: a journalctl that cannot run fails the
    real call too, and that failure is already ``available: false``."""
    controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine")
    assert len(journal.calls) == 1
    assert "--version" not in journal.calls[0]


def test_units_lists_names_and_descriptions(controls_client_as_operator, journal):
    body = controls_client_as_operator.get(UNITS).json()
    assert body["available"] is True
    assert body["units"] == [{"name": "robothor-engine", "description": "Genus OS Agent Engine"}]


@pytest.mark.parametrize(
    "unit",
    [
        "sshd",
        "robothor-nope",
        "../../etc/shadow",
        "robothor-engine.service",
        "robothor-engine;reboot",
        "robothor-alert@",
        "",
    ],
)
def test_a_unit_outside_the_allowlist_is_422_naming_the_set(
    controls_client_as_operator, journal, unit
):
    resp = controls_client_as_operator.get(f"{LOGS}?unit={unit}")
    assert resp.status_code == 422
    assert "robothor-engine" in resp.json()["detail"]
    assert journal.calls == [], "nothing may be executed for a unit we refuse"


# ── argv ─────────────────────────────────────────────────────────────────────


def test_argv_is_a_list_of_fixed_arguments(controls_client_as_operator, journal):
    controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&lines=25")
    argv = journal.calls[-1]
    assert argv[:9] == [
        "/usr/bin/journalctl",
        "-u",
        "robothor-engine",
        "-n",
        "25",
        "-o",
        "json",
        "--no-pager",
        "--output-fields=MESSAGE,PRIORITY,__REALTIME_TIMESTAMP",
    ]


@pytest.mark.parametrize("since", ["1h", "30m", "7d", "45s", "2026-09-01", "2026-09-01T10:00:00"])
def test_a_valid_since_is_passed_as_one_token(controls_client_as_operator, journal, since):
    controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&since={since}")
    argv = journal.calls[-1]
    passed = [a for a in argv if a.startswith("--since")]
    assert len(passed) == 1
    assert passed[0].startswith("--since=")
    assert " " not in passed[0].split("=", 1)[1] or since.startswith("2026")


@pytest.mark.parametrize(
    "since",
    [
        "--output=cat",
        "-f",
        "1h --output=export",
        "; reboot",
        "$(id)",
        "yesterday",
        "1y",
        "-1h",
    ],
)
def test_a_since_that_is_not_a_time_is_422(controls_client_as_operator, journal, since):
    """No argv position a caller can reach into, and no argument they choose."""
    resp = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&since={since}")
    assert resp.status_code == 422
    assert journal.calls == []


@pytest.mark.parametrize("lines", ["0", "1001", "-5", "abc", "1_0", "%20%2B5%20", "007"])
def test_lines_is_bounded(controls_client_as_operator, journal, lines):
    """``int("1_0")`` is 10 — a window the caller did not ask for."""
    resp = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&lines={lines}")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert journal.calls == []


def test_a_trailing_newline_does_not_smuggle_a_since_past_validation(
    controls_client_as_operator, journal
):
    """``$`` also matches before a trailing newline, so ``^…$`` with ``.match``
    admitted ``"1h\\n"`` — which then failed the ``since[-1] in 'smhd'`` test,
    lost its leading ``-``, and reached journald as an ABSOLUTE timestamp. The
    request quietly answered a different question from the one asked."""
    resp = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&since=1h%0A")
    assert resp.status_code == 422
    assert journal.calls == []


def test_a_trailing_newline_does_not_smuggle_a_unit_past_the_allowlist(
    controls_client_as_operator, journal
):
    resp = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine%0A")
    assert resp.status_code == 422
    assert journal.calls == []


def test_grep_is_never_passed_to_journalctl(controls_client_as_operator, journal):
    journal.state["stdout"] = _journal("engine started", "engine stopped")
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&grep=stopped").json()
    assert not any("stopped" in arg for arg in journal.calls[-1])
    assert [line["message"] for line in body["lines"]] == ["engine stopped"]


def test_grep_matches_the_redacted_line_not_the_raw_one(controls_client_as_operator, journal):
    """Otherwise ``grep`` is an oracle: a hit would confirm the secret's value
    in a line whose output is redacted."""
    journal.state["stdout"] = _journal(SECRET_LINE)
    body = controls_client_as_operator.get(
        f"{LOGS}?unit=robothor-engine&grep=sk-or-abc123def456ghi789"
    ).json()
    assert body["lines"] == []


# ── the payload ──────────────────────────────────────────────────────────────


def test_a_line_carries_a_timestamp_a_priority_and_a_message(controls_client_as_operator, journal):
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine").json()
    assert body["unit"] == "robothor-engine"
    assert body["available"] is True
    assert body["lines"][0]["message"] == "engine started"
    assert body["lines"][0]["priority"] == 6
    assert body["lines"][0]["ts"].startswith("20")


def test_a_secret_in_a_log_line_does_not_reach_the_browser(controls_client_as_operator, journal):
    journal.state["stdout"] = _journal(SECRET_LINE)
    resp = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine")
    assert "sk-or-abc123def456ghi789" not in resp.text
    assert "OPENROUTER_API_KEY" in resp.text, "the operator still sees WHICH variable"


def test_a_control_character_cannot_split_the_rendering(controls_client_as_operator, journal):
    journal.state["stdout"] = _journal("first\r\nSEP: forged")
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine").json()
    assert len(body["lines"]) == 1
    assert "\n" not in body["lines"][0]["message"]


def test_a_full_page_is_reported_as_truncated(controls_client_as_operator, journal):
    journal.state["stdout"] = _journal("a", "b")
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&lines=2").json()
    assert body["truncated"] is True
    assert (
        controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine&lines=9").json()["truncated"]
        is False
    )


def test_an_unparseable_journal_line_is_skipped_not_fatal(controls_client_as_operator, journal):
    journal.state["stdout"] = "not json\n" + _journal("engine started")
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine").json()
    assert [line["message"] for line in body["lines"]] == ["engine started"]


# ── when there is no journald ────────────────────────────────────────────────


def test_no_journalctl_is_available_false_with_a_reason(
    controls_client_as_operator, journal, monkeypatch
):
    """The container case. A 500 here reads as a broken appliance."""
    from routers import logs

    monkeypatch.setattr(logs, "_which", lambda _name: None)
    for path in (UNITS, f"{LOGS}?unit=robothor-engine"):
        body = controls_client_as_operator.get(path).json()
        assert body["available"] is False
        assert body["reason"].strip()
    assert controls_client_as_operator.get(UNITS).status_code == 200


def test_a_non_zero_probe_is_available_false(controls_client_as_operator, journal):
    journal.state["returncode"] = 1
    body = controls_client_as_operator.get(UNITS).json()
    assert body["available"] is False


def test_a_timeout_says_timed_out(controls_client_as_operator, journal):
    journal.state["raises"] = subprocess.TimeoutExpired("journalctl", 10)
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine").json()
    assert body["available"] is False
    assert "timed out" in body["reason"]
    assert body["lines"] == []


def test_the_read_carries_a_timeout(controls_client_as_operator, journal, monkeypatch):
    """A journalctl that never returns must not hold a worker thread forever."""
    from routers import logs

    seen: dict = {}

    def _run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, _journal("ok"), "")

    monkeypatch.setattr(logs, "_run", _run)
    controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine")
    assert seen["timeout"] == logs.JOURNAL_TIMEOUT_SECONDS
    assert seen.get("shell", False) is False


def test_a_failing_read_is_available_false_and_leaks_no_stderr(
    controls_client_as_operator, journal
):
    journal.state["returncode"] = 1
    journal.state["stdout"] = ""
    body = controls_client_as_operator.get(f"{LOGS}?unit=robothor-engine").json()
    assert body["available"] is False
    assert body["lines"] == []
