"""The settings API — the ``genus config`` brain, served over HTTP.

The contracts that matter, and why each is a test rather than a docstring:

* **One implementation, two surfaces.** ``genus config set X`` and
  ``PATCH /api/settings`` must not be able to disagree about what a value
  means, where it is written, or what needs restarting afterwards. They share
  ``robothor.settings.operator`` — asserted by function identity, because two
  functions that merely look alike are how this platform has repeatedly ended
  up with two answers to one question.
* **No secret leaves this API.** Not in the schema's ``default``, not in the
  values payload, not in an audit row. Asserted with a walker over the whole
  response rather than on the fields a test author remembered.
* **All-or-nothing.** One bad value in a PATCH writes nothing at all. A form
  that half-applies leaves the operator unable to say what the instance is
  now configured to do.
* **The environment wins, and says so.** A field an environment variable
  supplies is not editable here, and a PATCH naming it is refused with the
  reason — writing config.yaml underneath a variable that overrides it is the
  "applied, and nothing changed" failure the settings package exists to end.
"""

from __future__ import annotations

import json
import os

import pytest

SETTINGS = "/api/settings"
SCHEMA = "/api/settings/schema"

#: Non-secret, non-governed, hot (no restart): an int, so a bad value is easy.
HOT_INT = "ROBOTHOR_MAX_CONCURRENT_AGENTS"
#: Non-secret, non-governed, restart-required, two units.
RESTART_STR = "ROBOTHOR_LOG_DIR"
#: A governed guardrail flag — the flag store owns it, and it applies live.
GOVERNED = "ROBOTHOR_RBAC_MODE"
#: A credential. Never returned, never written.
SECRET = "ROBOTHOR_TELEGRAM_BOT_TOKEN"


@pytest.fixture(autouse=True)
def _no_flag_database(monkeypatch):
    """No settings test reads the box's ``feature_flags`` table.

    ``operator.db_rows`` swallows a database failure and returns ``{}``, so a
    test would pass on a box with no database and quietly assert something
    else on a box with one. Pinned empty; the governed tests patch it.
    """
    from robothor.settings import operator

    monkeypatch.setattr(operator, "db_rows", lambda: {})
    yield


@pytest.fixture
def audit_rows(monkeypatch):
    """Every audit event the routes write, captured at the one sink they use.

    Patched at ``routers._audit.log_event`` — the funnel — rather than at the
    router, so a route that starts auditing something new is covered without
    this fixture being edited.
    """
    rows: list[dict] = []

    def _log_event(event_type, **kwargs):
        rows.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr("routers._audit.log_event", _log_event)
    return rows


@pytest.fixture
def clean_env(monkeypatch):
    """None of the developer's own configuration for the names under test."""
    for name in (HOT_INT, RESTART_STR, GOVERNED, SECRET, "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    from robothor.settings import reset_settings

    reset_settings()
    yield
    reset_settings()


def _config_path(workspace):
    return workspace / ".robothor" / "config.yaml"


def _write_config(workspace, body: str):
    path = _config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    from robothor.settings import reset_settings

    reset_settings()
    return path


def _strings(payload) -> list[str]:
    """Every string anywhere in a JSON payload, keys and values alike."""
    if isinstance(payload, dict):
        return list(payload) + [s for v in payload.values() for s in _strings(v)]
    if isinstance(payload, list):
        return [s for v in payload for s in _strings(v)]
    return [payload] if isinstance(payload, str) else []


# ── the gate ─────────────────────────────────────────────────────────────────


def test_every_route_rejects_a_service_token(controls_client_as_service):
    """An agent's token must not read or write the instance's configuration."""
    assert controls_client_as_service.get(SCHEMA).status_code == 403
    assert controls_client_as_service.get(SETTINGS).status_code == 403
    assert (
        controls_client_as_service.patch(SETTINGS, json={"changes": {HOT_INT: 4}}).status_code
        == 403
    )


def test_a_non_operator_human_is_rejected(controls_client_as_viewer):
    assert controls_client_as_viewer.get(SETTINGS).status_code == 403
    assert controls_client_as_viewer.patch(SETTINGS, json={"changes": {}}).status_code == 403


def test_an_operator_of_another_tenant_is_rejected(controls_client_as_other_tenant_owner):
    assert controls_client_as_other_tenant_owner.get(SCHEMA).status_code == 403


# ── schema ───────────────────────────────────────────────────────────────────


def test_schema_lists_every_declared_field_exactly_once(controls_client_as_operator):
    from robothor.settings.registry import field_index

    body = controls_client_as_operator.get(SCHEMA).json()
    names = [f["name"] for group in body["groups"] for f in group["fields"]]
    declared = {record["env"] for record in field_index().values()}
    assert len(names) == len(set(names)), "a field is listed twice — aliases are not fields"
    assert set(names) == declared


def test_schema_groups_follow_the_registry_and_are_deterministic(controls_client_as_operator):
    from robothor.settings.registry import groups

    first = controls_client_as_operator.get(SCHEMA).json()
    second = controls_client_as_operator.get(SCHEMA).json()
    assert first == second, "two identical requests must produce byte-identical schemas"
    assert [g["id"] for g in first["groups"]] == list(groups())
    assert all(g["label"] for g in first["groups"])


def test_schema_carries_the_declared_metadata_for_a_field(controls_client_as_operator):
    body = controls_client_as_operator.get(SCHEMA).json()
    field = next(f for group in body["groups"] for f in group["fields"] if f["name"] == RESTART_STR)
    assert field["env"] == RESTART_STR
    assert field["type"] == "str"
    assert field["description"]
    assert field["default"] == "/var/log/robothor"
    assert field["secret"] is False
    assert field["governed"] is False
    assert field["restart_required"] is True
    assert field["restart_units"] == ["robothor-engine", "robothor-bridge"]
    assert field["since"]
    assert field["hot"] is False


def test_schema_marks_a_live_setting_hot(controls_client_as_operator):
    body = controls_client_as_operator.get(SCHEMA).json()
    field = next(f for g in body["groups"] for f in g["fields"] if f["name"] == HOT_INT)
    assert field["restart_required"] is False
    assert field["hot"] is True


def test_schema_gives_a_governed_flag_its_enum_and_calls_it_hot(controls_client_as_operator):
    from robothor.flags.store import valid_values_for

    body = controls_client_as_operator.get(SCHEMA).json()
    field = next(f for g in body["groups"] for f in g["fields"] if f["name"] == GOVERNED)
    assert field["governed"] is True
    assert field["enum"] == list(valid_values_for(GOVERNED))
    assert field["hot"] is True, "a governed flag applies through the flag store, live"


def test_schema_omits_enum_for_a_field_with_no_value_set(controls_client_as_operator):
    body = controls_client_as_operator.get(SCHEMA).json()
    field = next(f for g in body["groups"] for f in g["fields"] if f["name"] == HOT_INT)
    assert "enum" not in field


def test_schema_never_carries_a_secret_default(controls_client_as_operator, monkeypatch):
    monkeypatch.setenv(SECRET, "super-secret-token-value")
    body = controls_client_as_operator.get(SCHEMA).json()
    assert "super-secret-token-value" not in json.dumps(body)
    field = next(f for g in body["groups"] for f in g["fields"] if f["name"] == SECRET)
    assert field["secret"] is True
    assert field["default"] is None


# ── values ───────────────────────────────────────────────────────────────────


def test_values_report_a_default_as_a_default(controls_client_as_operator, clean_env):
    body = controls_client_as_operator.get(SETTINGS).json()
    entry = body["values"][HOT_INT]
    assert entry["value"] == 3
    assert entry["source"] == "default"
    assert entry["editable"] is True


def test_values_report_config_yaml_as_the_source(
    controls_client_as_operator, clean_env, env_workspace
):
    _write_config(env_workspace, "settings:\n  engine:\n    max_concurrent_agents: 7\n")
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][HOT_INT]
    assert entry["value"] == 7
    assert entry["source"] == "config"
    assert entry["editable"] is True


def test_an_env_overridden_field_is_not_editable(
    controls_client_as_operator, clean_env, monkeypatch
):
    monkeypatch.setenv(HOT_INT, "9")
    from robothor.settings import reset_settings

    reset_settings()
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][HOT_INT]
    assert entry["value"] == 9
    assert entry["source"] == "env"
    assert entry["editable"] is False, "the variable wins; the form must show a badge, not a box"


def test_a_governed_flag_reports_its_database_row(controls_client_as_operator, monkeypatch):
    from robothor.settings import operator

    monkeypatch.setattr(operator, "db_rows", lambda: {GOVERNED: "enforce"})
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][GOVERNED]
    assert entry["value"] == "enforce"
    assert entry["source"] == "db"
    assert entry["editable"] is True


def test_a_secret_is_reported_as_configured_and_a_fingerprint_only(
    controls_client_as_operator, clean_env, monkeypatch
):
    import hashlib

    monkeypatch.setenv(SECRET, "super-secret-token-value")
    from robothor.settings import reset_settings

    reset_settings()
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][SECRET]
    assert entry["value"]["configured"] is True
    expected = hashlib.sha256(b"super-secret-token-value").hexdigest()[:8]
    assert entry["value"]["fingerprint"] == f"sha256:{expected}"
    assert entry["editable"] is False


def test_an_unconfigured_secret_reports_no_fingerprint(controls_client_as_operator, clean_env):
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][SECRET]
    assert entry["value"] == {"configured": False, "fingerprint": None}


def test_no_secret_value_appears_anywhere_in_the_values_payload(
    controls_client_as_operator, clean_env, monkeypatch
):
    """A walker, not a field list: the leak this guards is the field nobody
    remembered to check."""
    from robothor.settings.registry import field_index

    planted = {}
    seen = set()
    for record in field_index().values():
        if not record["secret"] or record["env"] in seen:
            continue
        seen.add(record["env"])
        planted[record["env"]] = f"leak-canary-{record['env'].lower()}"
        monkeypatch.setenv(record["env"], planted[record["env"]])
    from robothor.settings import reset_settings

    reset_settings()

    body = controls_client_as_operator.get(SETTINGS).json()
    haystack = json.dumps(body)
    leaked = sorted(name for name, value in planted.items() if value in haystack)
    assert not leaked, f"the settings API served these credentials: {leaked}"


def test_pending_restart_names_the_units_of_a_setting_the_environment_overrides(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    """config.yaml says one thing and this process reads another — the doctor's
    ``config.pending_restart`` rule, answered over HTTP."""
    _write_config(env_workspace, 'settings:\n  paths:\n    log_dir: "/var/log/other"\n')
    monkeypatch.setenv(RESTART_STR, "/var/log/robothor")
    from robothor.settings import reset_settings

    reset_settings()
    body = controls_client_as_operator.get(SETTINGS).json()
    assert body["pending_restart"] == ["robothor-bridge", "robothor-engine"]


def test_pending_restart_is_empty_when_nothing_is_overridden(
    controls_client_as_operator, clean_env
):
    assert controls_client_as_operator.get(SETTINGS).json()["pending_restart"] == []


# ── patch ────────────────────────────────────────────────────────────────────


def test_patch_writes_config_yaml_and_preserves_unknown_keys(
    controls_client_as_operator, clean_env, env_workspace
):
    path = _write_config(
        env_workspace,
        "# an operator's own header\n"
        "federation:\n"
        "  instance_id: alpha\n"
        "settings:\n"
        "  engine:\n"
        "    max_concurrent_agents: 3\n",
    )
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {HOT_INT: 8}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == [HOT_INT]
    assert body["errors"] == []
    assert body["pending_restart"] == [], "this setting applies live"

    text = path.read_text(encoding="utf-8")
    assert "max_concurrent_agents: 8" in text
    assert "# an operator's own header" in text, "a textual edit must keep comments"
    assert "instance_id: alpha" in text, "keys outside the settings block must survive"


def test_patch_leaves_no_temporary_file_behind(
    controls_client_as_operator, clean_env, env_workspace
):
    """The write is temp-file-plus-rename; the temp file must not survive it."""
    _write_config(env_workspace, "settings:\n  engine:\n    max_concurrent_agents: 3\n")
    controls_client_as_operator.patch(SETTINGS, json={"changes": {HOT_INT: 8}})
    leftovers = [p.name for p in (env_workspace / ".robothor").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_patch_is_all_or_nothing_on_one_bad_value(
    controls_client_as_operator, clean_env, env_workspace
):
    path = _write_config(env_workspace, "settings:\n  engine:\n    max_concurrent_agents: 3\n")
    before = path.read_text(encoding="utf-8")

    r = controls_client_as_operator.patch(
        SETTINGS,
        json={"changes": {RESTART_STR: "/var/log/new", HOT_INT: "not-a-number"}},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["applied"] == []
    assert [e["name"] for e in body["errors"]] == [HOT_INT]
    assert "not-a-number" in body["errors"][0]["message"]
    assert path.read_text(encoding="utf-8") == before, "the valid change must not have landed"


def test_patch_reports_every_error_not_just_the_first(
    controls_client_as_operator, clean_env, env_workspace
):
    _write_config(env_workspace, "settings:\n")
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {HOT_INT: "nope", "ROBOTHOR_NOT_A_SETTING": "x"}}
    )
    assert r.status_code == 422
    assert {e["name"] for e in r.json()["errors"]} == {HOT_INT, "ROBOTHOR_NOT_A_SETTING"}


def test_patch_rejects_an_unknown_name_with_a_suggestion(controls_client_as_operator, clean_env):
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {"ROBOTHOR_MAX_CONCURRENT_AGENT": 4}}
    )
    assert r.status_code == 422
    message = r.json()["errors"][0]["message"]
    assert "no such setting" in message
    assert HOT_INT in message, "a near-miss must name the setting the operator meant"


def test_patch_reports_the_units_that_need_a_restart(
    controls_client_as_operator, clean_env, env_workspace
):
    _write_config(env_workspace, "settings:\n")
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {RESTART_STR: "/var/log/x"}})
    assert r.status_code == 200, r.text
    assert r.json()["pending_restart"] == ["robothor-bridge", "robothor-engine"]


def test_patch_routes_a_governed_flag_to_the_flag_store_and_reports_it_hot(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    written = {}

    def _set_flag(name, value, actor, reason):
        written["call"] = (name, value, actor, reason)

    monkeypatch.setattr("robothor.flags.store.set_flag", _set_flag)
    path = _config_path(env_workspace)

    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {GOVERNED: "enforce"}, "note": "promoting rbac"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"applied": [GOVERNED], "pending_restart": [], "errors": []}
    assert written["call"][:2] == (GOVERNED, "enforce")
    assert written["call"][2].startswith("operator:")
    assert not path.exists(), "a governed flag must never be written to config.yaml"


def test_patch_refuses_a_value_a_governed_flag_does_not_honour(
    controls_client_as_operator, clean_env
):
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {GOVERNED: "true"}})
    assert r.status_code == 422
    assert "observe" in r.json()["errors"][0]["message"]


def test_patch_refuses_a_field_the_environment_overrides(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    monkeypatch.setenv(HOT_INT, "9")
    from robothor.settings import reset_settings

    reset_settings()
    path = _config_path(env_workspace)

    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {HOT_INT: 4}})
    assert r.status_code == 422
    message = r.json()["errors"][0]["message"]
    assert HOT_INT in message
    assert "environment" in message and "wins" in message
    assert not path.exists(), "nothing may be written under a variable that overrides it"


def test_patch_refuses_to_write_a_secret(controls_client_as_operator, clean_env, env_workspace):
    path = _config_path(env_workspace)
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {SECRET: "a-new-token"}})
    assert r.status_code == 422
    message = r.json()["errors"][0]["message"]
    assert "credential" in message
    assert "a-new-token" not in message, "a refusal must not echo the credential back"
    assert not path.exists()


def test_a_refused_secret_write_never_reaches_the_audit_log(
    controls_client_as_operator, clean_env, audit_rows
):
    controls_client_as_operator.patch(SETTINGS, json={"changes": {SECRET: "a-new-token"}})
    assert all("a-new-token" not in s for row in audit_rows for s in _strings(row))


def test_an_applied_change_is_audited_by_name_and_never_by_value(
    controls_client_as_operator, clean_env, env_workspace, audit_rows
):
    _write_config(env_workspace, "settings:\n")
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {RESTART_STR: "/var/log/canary-value"}, "note": "tidying"}
    )
    assert r.status_code == 200, r.text
    assert audit_rows, "a write to the instance's configuration must leave a trail"
    row = audit_rows[-1]
    flat = _strings(row)
    assert RESTART_STR in flat, "the audit row must name what changed"
    assert all("/var/log/canary-value" not in s for s in flat), (
        "the audit log is exported to a SIEM; it records names, never values"
    )


def test_an_empty_patch_changes_nothing_and_says_so(controls_client_as_operator, clean_env):
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {}})
    assert r.status_code == 200
    assert r.json() == {"applied": [], "pending_restart": [], "errors": []}


def test_a_patched_value_is_what_the_next_get_reports(
    controls_client_as_operator, clean_env, env_workspace
):
    """The process caches its settings; a surface that writes must re-read.

    Otherwise the form the operator just saved reverts on reload, and the
    instance and the page disagree about what it is configured to do.
    """
    _write_config(env_workspace, "settings:\n")
    # The page loads first, which is what fills this process's settings cache —
    # patching into a cold cache would pass with or without the re-read.
    assert controls_client_as_operator.get(SETTINGS).json()["values"][HOT_INT]["value"] == 3

    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {HOT_INT: 11}})
    assert r.status_code == 200, r.text
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][HOT_INT]
    assert entry["value"] == 11
    assert entry["source"] == "config"


# ── one implementation ───────────────────────────────────────────────────────


def test_the_router_and_the_cli_call_the_same_functions():
    """Identity, not similarity. ``genus config set X`` and ``PATCH
    /api/settings`` route by the same metadata, coerce with the same coercer
    and write through the same writer, so they cannot disagree."""
    from routers import settings as router

    from robothor.cli import config_cmd
    from robothor.settings import operator

    assert router.operator is operator
    for attr, shared in (
        ("_record", operator.record),
        ("_coerce", operator.coerce),
        ("_resolve", operator.resolve),
        ("_units_for", operator.units_for),
        ("_db_value", operator.db_value),
    ):
        assert getattr(config_cmd, attr) is shared, f"config_cmd.{attr} drifted from the library"


def test_the_route_handlers_stay_synchronous():
    """psycopg2 and file I/O belong in the threadpool — see
    ``test_route_concurrency.py``, which this keeps green by construction."""
    import inspect

    from routers import settings as router

    for route in router.router.routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


# ── I1/I2: the Flags page must be able to save what it displays ──────────────


def test_every_governed_value_is_one_the_same_api_would_accept(
    controls_client_as_operator, clean_env
):
    """GET -> PATCH must round-trip for all 21 flags.

    ``enum`` comes from the flag store and is always strings; the value used to
    come from the pydantic-typed default, which for a boolean flag is JSON
    ``false`` and for ROBOTHOR_SANDBOX_DEFAULT_MODE is ``""``. A ``<select>``
    populated from ``enum`` cannot preselect either, and saving the form
    unchanged was a 422.
    """
    from robothor.flags.store import GOVERNED_FLAGS

    schema = controls_client_as_operator.get(SCHEMA).json()
    fields = {f["name"]: f for g in schema["groups"] for f in g["fields"]}
    values = controls_client_as_operator.get(SETTINGS).json()["values"]

    offenders = []
    for name in sorted(GOVERNED_FLAGS):
        field = fields[name]
        for label, candidate in (("value", values[name]["value"]), ("default", field["default"])):
            if candidate not in field["enum"]:
                offenders.append((name, label, candidate, field["enum"]))
    assert not offenders, offenders


def test_patching_back_what_get_returned_is_never_refused(
    controls_client_as_operator, clean_env, monkeypatch
):
    monkeypatch.setattr("robothor.flags.store.set_flag", lambda *a, **k: None)
    values = controls_client_as_operator.get(SETTINGS).json()["values"]

    from robothor.flags.store import GOVERNED_FLAGS

    changes = {name: values[name]["value"] for name in sorted(GOVERNED_FLAGS)}
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": changes})
    assert r.status_code == 200, r.text


def test_the_settings_page_and_the_controls_page_agree_on_every_flag(
    controls_client_as_operator, clean_env, fake_verdict
):
    """Two surfaces, one question. The engine reads ``robothor.flags.store``:
    DB row -> environment -> the flag's own default. config.yaml is not a layer
    for a governed flag at all, so resolving one through the settings
    precedence reported a value the engine never reads -- ROBOTHOR_DNC_MODE in
    particular, which Controls knows is floored at ``enforce`` and the settings
    page called ``observe``.
    """
    controls = {
        flag["name"]: flag["value"]
        for flag in controls_client_as_operator.get("/api/controls").json()
    }
    values = controls_client_as_operator.get(SETTINGS).json()["values"]

    disagree = {
        name: (values[name]["value"], shown)
        for name, shown in controls.items()
        if values[name]["value"] != shown
    }
    assert not disagree, disagree


def test_config_yaml_is_not_a_layer_for_a_governed_flag(
    controls_client_as_operator, clean_env, env_workspace
):
    """An operator can put ``flags.rbac_mode`` in config.yaml. The engine will
    never read it, so neither may this page."""
    _write_config(env_workspace, "settings:\n  flags:\n    rbac_mode: enforce\n")
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][GOVERNED]
    assert entry["value"] == "observe", "config.yaml is not a layer the flag store reads"
    assert entry["source"] == "default"


def test_a_governed_flag_set_in_the_environment_reports_env(
    controls_client_as_operator, clean_env, monkeypatch
):
    monkeypatch.setenv(GOVERNED, "alert")
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][GOVERNED]
    assert entry["value"] == "alert"
    assert entry["source"] == "env"


# ── I3: the config half of a batch is one atomic replace ─────────────────────


def test_a_config_batch_is_written_in_exactly_one_replace(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    from pathlib import Path

    _write_config(env_workspace, "settings:\n")
    replaces: list[object] = []
    real = Path.replace

    def _counting(self, target):
        replaces.append(target)
        return real(self, target)

    monkeypatch.setattr(Path, "replace", _counting)
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {HOT_INT: 8, RESTART_STR: "/var/log/two"}}
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["applied"]) == sorted([HOT_INT, RESTART_STR])
    assert len(replaces) == 1, "a batch must be one replace, not one per field"


def test_a_failure_part_way_through_a_batch_leaves_the_file_byte_identical(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    """The commit says "atomic patch"; per-field replaces made it atomic per
    FIELD, so a failure on the second field left the first one written and the
    operator could not say what the instance was now configured to do."""
    from pathlib import Path

    path = _write_config(env_workspace, "settings:\n  engine:\n    max_concurrent_agents: 3\n")
    before = path.read_bytes()

    def _boom(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", _boom)
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {HOT_INT: 8, RESTART_STR: "/var/log/two"}}
    )
    assert r.status_code == 500, r.text
    body = r.json()
    assert body["applied"] == [], "nothing landed, so nothing may be reported as applied"
    assert {e["name"] for e in body["errors"]} == {HOT_INT, RESTART_STR}
    assert path.read_bytes() == before
    assert [p.name for p in (env_workspace / ".robothor").iterdir()] == ["config.yaml"]


# ── C1 over HTTP: a 200 must never leave an unparseable config.yaml ──────────


def test_a_yaml_indicator_in_a_value_round_trips_rather_than_breaking_the_file(
    controls_client_as_operator, clean_env, env_workspace
):
    """``- item`` used to be written bare. The file stopped parsing, the
    operator was told the setting applied, and the page they would use to undo
    it started returning 500."""
    path = _write_config(env_workspace, "settings:\n")
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {RESTART_STR: "- item"}})
    assert r.status_code == 200, r.text

    import yaml

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["settings"]["paths"]["log_dir"] == "- item"
    # And the page still renders afterwards.
    after = controls_client_as_operator.get(SETTINGS)
    assert after.status_code == 200
    assert after.json()["values"][RESTART_STR]["value"] == "- item"


# ── I4: a credential is refused as a credential, whatever supplies it ────────


def test_an_env_supplied_secret_is_refused_as_a_secret_not_as_an_override(
    controls_client_as_operator, clean_env, monkeypatch
):
    """Secrets are the field class most likely to be env-supplied, so the
    env-override sentence -- "clear the variable on the box and restart ..." --
    was the common answer, and it is both false (the vault owns credentials)
    and harmful (the running services need that variable)."""
    monkeypatch.setenv(SECRET, "already-in-the-environment")
    from robothor.settings import reset_settings

    reset_settings()
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": {SECRET: "a-new-token"}})
    assert r.status_code == 422
    message = r.json()["errors"][0]["message"]
    assert "credential" in message
    assert "Clear the variable" not in message
    assert "a-new-token" not in message and "already-in-the-environment" not in message


# ── minors ───────────────────────────────────────────────────────────────────


def test_a_name_and_its_deprecated_alias_in_one_batch_is_an_error(
    controls_client_as_operator, clean_env, env_workspace
):
    """Two keys, one field: written, they are last-one-wins by dict order and
    the response names the field twice."""
    path = _config_path(env_workspace)
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {"ROBOTHOR_CODEX_HOME": "/one", "CODEX_HOME": "/two"}}
    )
    assert r.status_code == 422
    assert "same setting" in r.json()["errors"][0]["message"]
    assert not path.exists()


def test_a_batch_larger_than_the_cap_is_refused_before_any_work(
    controls_client_as_operator, clean_env
):
    from routers.settings import MAX_CHANGES

    changes = {f"ROBOTHOR_NOT_A_SETTING_{i}": "x" for i in range(MAX_CHANGES + 1)}
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": changes})
    assert r.status_code == 422


def test_an_oversized_note_is_refused(controls_client_as_operator, clean_env):
    from routers.settings import MAX_NOTE

    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {}, "note": "x" * (MAX_NOTE + 1)}
    )
    assert r.status_code == 422


def test_a_rejected_key_is_never_written_verbatim_into_the_audit_log(
    controls_client_as_operator, clean_env, audit_rows
):
    """ "Names only" is true; "names from the registry" was not. A typo'd
    credential in a key would otherwise become a durable SIEM record."""
    secretish = "sk-live-DEADBEEF-not-a-setting"
    controls_client_as_operator.patch(SETTINGS, json={"changes": {secretish: "x"}})
    assert audit_rows
    flat = _strings(audit_rows[-1])
    assert all(secretish not in s for s in flat)
    assert audit_rows[-1]["details"]["unknown_names"] == 1


# ── N1: both pages against the ENGINE, not against each other ────────────────

#: Spellings an operator actually types. ``feature_flags.py``'s own docstring
#: tells them to use ``systemctl set-environment ROBOTHOR_RIP_1_ENABLED=1``,
#: and the engine lowercases before it reads, so every one of these is a value
#: the engine honours and a page must not contradict.
ENV_SPELLINGS = [
    "1",
    "0",
    "true",
    "TRUE",
    "True",
    "yes",
    "on",
    "off",
    "no",
    "false",
    "Observe",
    "OBSERVE",
    "observe",
    "Enforce",
    "ALERT",
    "alert",
    "  enforce  ",
    "mostly",
    "",
]


#: ``(flag, raw)`` pairs where the ENGINE itself disagrees with the value set
#: its own API advertises. NOT a bug in these pages, and deliberately not fixed
#: here: closing it means changing what a guardrail DOES.
#:
#: ``ROBOTHOR_RIP_7_MODE=off`` is the one. ``valid_values_for`` offers ``off``
#: for every ``*_MODE`` flag, ``/api/controls`` accepts, persists and audits it,
#: and ``rip_7_enforcement_mode()`` then reads it as ``observe`` -- the exact
#: inert-de-escalation-lever defect that ``_generic_mode`` 's own comment says
#: was fixed for the ladder flags. Honouring it would take a guardrail from
#: observing to DARK, which is an operator's decision, not a fix round's.
KNOWN_ENGINE_DISAGREEMENTS = {("ROBOTHOR_RIP_7_MODE", "off")}


def _engine_says(name, reader, gate, raw, monkeypatch):
    """What the ENGINE resolves for ``name`` with ``raw`` in the environment."""
    from robothor.engine import feature_flags as ff

    if raw is not None:
        monkeypatch.setenv(name, raw)
    else:
        monkeypatch.delenv(name, raising=False)
    if gate:
        monkeypatch.setenv(gate, "1")
    value = reader()
    del ff
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


@pytest.mark.parametrize("raw", ENV_SPELLINGS)
def test_both_pages_report_what_the_engine_resolves_for_every_flag(
    raw, controls_client_as_operator, engine_readers, fake_verdict, monkeypatch
):
    """The third party is the engine. Two pages agreeing with each other proves
    nothing -- ``normalise`` was wrong once and both pages were wrong together,
    reporting a guardrail ``false`` while the engine ran it and a compliance
    opt-out ``enforce`` while the engine only observed.

    So each page is compared against the accessor the ENGINE calls, for every
    governed flag, across the spellings an operator actually types. The flag
    store is pinned to "no DB row" on both sides so the environment is the only
    variable.
    """
    from robothor.flags import store

    monkeypatch.setattr(store, "resolve", lambda name: os.environ.get(name))

    wrong = []
    for name, (reader, gate) in sorted(engine_readers.items()):
        with monkeypatch.context() as m:
            engine = _engine_says(name, reader, gate, raw, m)
            values = controls_client_as_operator.get(SETTINGS).json()["values"]
            controls = {
                flag["name"]: flag["value"]
                for flag in controls_client_as_operator.get("/api/controls").json()
            }
        if (name, raw) in KNOWN_ENGINE_DISAGREEMENTS:
            continue
        if values[name]["value"] != engine:
            wrong.append(("settings", name, raw, values[name]["value"], engine))
        if controls[name] != engine:
            wrong.append(("controls", name, raw, controls[name], engine))
    assert not wrong, wrong


def test_the_known_engine_disagreements_are_still_exactly_these(engine_readers, monkeypatch):
    """A skip list that is not itself pinned is a skip list that grows.

    Each entry must STILL disagree; when the engine is fixed this test fails
    and the entry is deleted, rather than sitting here forever describing a
    defect that no longer exists.
    """
    from robothor.flags import store

    monkeypatch.setattr(store, "resolve", lambda name: os.environ.get(name))
    for name, raw in sorted(KNOWN_ENGINE_DISAGREEMENTS):
        reader, gate = engine_readers[name]
        with monkeypatch.context() as m:
            engine = _engine_says(name, reader, gate, raw, m)
            shown = store.normalise(name, raw)
        assert shown != engine, (
            f"{name}={raw!r} now agrees ({shown!r}) — delete it from "
            "KNOWN_ENGINE_DISAGREEMENTS and from the report"
        )


def test_the_store_spells_a_value_the_way_the_engine_reads_one():
    """``normalise`` is the one place both pages go through, so it carries the
    engine's parsing rules -- case-insensitive, and the same truthy set."""
    from robothor.flags import store

    for raw in ("1", "TRUE", "True", "yes", "YES", "on", "  true  "):
        assert store.normalise("ROBOTHOR_RIP_1_ENABLED", raw) == "true", raw
    for raw in ("0", "false", "FALSE", "no", "off", "nonsense"):
        assert store.normalise("ROBOTHOR_RIP_1_ENABLED", raw) == "false", raw
    for raw, expected in (
        ("Observe", "observe"),
        ("ENFORCE", "enforce"),
        ("  Alert ", "alert"),
        ("off", "off"),
    ):
        assert store.normalise("ROBOTHOR_RBAC_MODE", raw) == expected, raw


def test_the_engine_and_the_store_share_one_truthy_set():
    """Identity, not equality: a second copy is a second answer waiting to
    happen, and the third copy in ``scripts/flag_audit.py`` was exactly that."""
    from robothor.engine import feature_flags as ff
    from robothor.flags.store import TRUE_VALUES

    assert ff._TRUE_VALUES is TRUE_VALUES


# ── N5: what a page shows for a value outside the set ────────────────────────


def test_a_value_the_flag_does_not_accept_is_masked_by_the_engines_default(
    controls_client_as_operator, clean_env, monkeypatch
):
    """Not just "the two pages agree" -- what they agree ON. A typo in a
    variable must read as the engine's own fallback, which for the compliance
    opt-out means enforcing."""
    monkeypatch.setenv(GOVERNED, "mostly")
    monkeypatch.setenv("ROBOTHOR_DNC_MODE", "whatever")
    values = controls_client_as_operator.get(SETTINGS).json()["values"]
    assert values[GOVERNED]["value"] == "observe"
    assert values["ROBOTHOR_DNC_MODE"]["value"] == "enforce"


# ── N2: a failed config write stops the governed half ────────────────────────


def test_a_failed_config_write_leaves_the_flag_store_untouched(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    """All-or-nothing across BOTH stores when the file is what failed.

    A file and a table cannot be one transaction, so the file goes first and a
    failure there must stop the batch. The fix notes claimed this; the code
    caught the error and ran the governed loop anyway, so a 500 could still
    flip a guardrail.
    """
    from pathlib import Path

    writes: list[tuple] = []
    monkeypatch.setattr("robothor.flags.store.set_flag", lambda *a, **k: writes.append(a))
    _write_config(env_workspace, "settings:\n")

    def _boom(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", _boom)
    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {HOT_INT: 8, GOVERNED: "enforce"}}
    )
    assert r.status_code == 500, r.text
    assert r.json()["applied"] == []
    assert writes == [], "a guardrail was flipped by a request that failed to write the file"


# ── N3: every 422 from this route has the same flat body ─────────────────────


def test_an_over_limit_batch_returns_the_same_flat_body_as_a_validation_error(
    controls_client_as_operator, clean_env
):
    """The report tells B16b ``resp.json().errors`` works on the 422. A
    pydantic ``Field`` cap returns FastAPI's ``{"detail": [...]}`` instead, so
    the one shape the UI was promised had two exceptions in it."""
    from routers.settings import MAX_CHANGES

    changes = {f"ROBOTHOR_NOT_A_SETTING_{i}": "x" for i in range(MAX_CHANGES + 1)}
    r = controls_client_as_operator.patch(SETTINGS, json={"changes": changes})
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"applied", "pending_restart", "errors"}
    assert body["applied"] == []
    assert body["errors"] and "at most" in body["errors"][0]["message"]


def test_an_over_limit_note_returns_the_same_flat_body(controls_client_as_operator, clean_env):
    from routers.settings import MAX_NOTE

    r = controls_client_as_operator.patch(
        SETTINGS, json={"changes": {}, "note": "x" * (MAX_NOTE + 1)}
    )
    assert r.status_code == 422
    body = r.json()
    assert set(body) == {"applied", "pending_restart", "errors"}
    assert body["errors"][0]["name"] == "note"


def test_every_refusal_this_route_makes_has_the_same_three_keys(
    controls_client_as_operator, clean_env, monkeypatch
):
    """One walker over every 422 this route can produce, so a new refusal
    cannot quietly ship a fourth body shape."""
    from routers.settings import MAX_CHANGES, MAX_NOTE

    monkeypatch.setenv(HOT_INT, "9")
    from robothor.settings import reset_settings

    reset_settings()
    payloads = [
        {"changes": {"ROBOTHOR_NOT_A_SETTING": "x"}},  # unknown name
        {"changes": {SECRET: "t"}},  # secret
        {"changes": {GOVERNED: "mostly"}},  # not a rung
        {"changes": {HOT_INT: 4}},  # env override
        {"changes": {RESTART_STR: object}},  # unserialisable -> bad type
        {"changes": {"ROBOTHOR_CODEX_HOME": "/a", "CODEX_HOME": "/b"}},  # duplicate
        {"changes": {f"ROBOTHOR_NOPE_{i}": "x" for i in range(MAX_CHANGES + 1)}},
        {"changes": {}, "note": "x" * (MAX_NOTE + 1)},
    ]
    for payload in payloads:
        if payload["changes"].get(RESTART_STR) is object:
            payload = {"changes": {RESTART_STR: {"not": "a scalar"}}}
        r = controls_client_as_operator.patch(SETTINGS, json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
        assert set(r.json()) == {"applied", "pending_restart", "errors"}, payload
        assert r.json()["errors"], payload


# ── round 3: `editable` and the PATCH must agree, and say why ────────────────


def test_a_governed_flag_is_editable_even_when_the_environment_sets_it(
    controls_client_as_operator, clean_env, monkeypatch
):
    """The flag store outranks the environment for a governed flag.

    ``store.resolve`` reads the DB row first and only falls back to
    ``os.environ``, so writing one is NOT invisible the way a config.yaml write
    under a variable is. ``PATCH /api/controls`` has always accepted it and
    ``_plan`` exempts governed flags from the env refusal -- so a GET that said
    ``editable: false`` was the one surface disagreeing, and the Flags page
    would have greyed out a control that works.
    """
    monkeypatch.setenv(GOVERNED, "alert")
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][GOVERNED]
    assert entry["source"] == "env"
    assert entry["editable"] is True
    assert entry["reason"] is None


def test_what_get_calls_editable_is_exactly_what_patch_accepts(
    controls_client_as_operator, clean_env, env_workspace, monkeypatch
):
    """One walker over every field, in a state where several are refused.

    A form that greys out a field the API would accept, or offers one it would
    refuse, is a page that has to reimplement the write path to be right. This
    asserts the two answers are the same answer.
    """
    monkeypatch.setattr("robothor.flags.store.set_flag", lambda *a, **k: None)
    monkeypatch.setenv(HOT_INT, "9")
    monkeypatch.setenv(SECRET, "already-set")
    monkeypatch.setenv(GOVERNED, "alert")
    from robothor.settings import reset_settings

    reset_settings()

    values = controls_client_as_operator.get(SETTINGS).json()["values"]
    schema = controls_client_as_operator.get(SCHEMA).json()
    fields = {f["name"]: f for g in schema["groups"] for f in g["fields"]}

    for name in (HOT_INT, SECRET, GOVERNED, RESTART_STR):
        sample = fields[name]["enum"][0] if "enum" in fields[name] else "x"
        r = controls_client_as_operator.patch(SETTINGS, json={"changes": {name: sample}})
        accepted = r.status_code == 200
        assert values[name]["editable"] is accepted, (
            f"{name}: GET says editable={values[name]['editable']} and PATCH "
            f"returned {r.status_code}"
        )
        if not accepted:
            assert values[name]["reason"] == r.json()["errors"][0]["message"], (
                f"{name}: the reason GET gives is not the sentence PATCH returns"
            )
        else:
            assert values[name]["reason"] is None


def test_an_env_overridden_field_carries_the_env_sentence(
    controls_client_as_operator, clean_env, monkeypatch
):
    monkeypatch.setenv(HOT_INT, "9")
    from robothor.settings import reset_settings

    reset_settings()
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][HOT_INT]
    assert entry["editable"] is False
    assert HOT_INT in entry["reason"]
    assert "wins over config.yaml" in entry["reason"]


def test_a_secret_carries_the_secrets_are_elsewhere_sentence(
    controls_client_as_operator, clean_env, monkeypatch
):
    """Even when the environment supplies it: a credential is refused as a
    credential, and the reason must not tell the operator to clear a variable
    the running services need."""
    monkeypatch.setenv(SECRET, "already-in-the-environment")
    from robothor.settings import reset_settings

    reset_settings()
    entry = controls_client_as_operator.get(SETTINGS).json()["values"][SECRET]
    assert entry["editable"] is False
    assert "credential" in entry["reason"]
    assert "Clear the variable" not in entry["reason"]
    assert "already-in-the-environment" not in entry["reason"]


def test_every_value_entry_carries_the_same_four_keys(controls_client_as_operator, clean_env):
    values = controls_client_as_operator.get(SETTINGS).json()["values"]
    assert values
    for name, entry in values.items():
        assert set(entry) == {"value", "source", "editable", "reason"}, name
        assert (entry["reason"] is None) is entry["editable"], name


def test_no_reason_sentence_ever_carries_a_secret_value(
    controls_client_as_operator, clean_env, monkeypatch
):
    """``reason`` is new prose on the read path, so it goes under the same
    canary walk as everything else this API serves."""
    from robothor.settings.registry import field_index

    planted = {}
    seen = set()
    for record in field_index().values():
        if not record["secret"] or record["env"] in seen:
            continue
        seen.add(record["env"])
        planted[record["env"]] = f"reason-canary-{record['env'].lower()}"
        monkeypatch.setenv(record["env"], planted[record["env"]])
    from robothor.settings import reset_settings

    reset_settings()

    reasons = json.dumps(
        [e["reason"] for e in controls_client_as_operator.get(SETTINGS).json()["values"].values()]
    )
    leaked = sorted(name for name, value in planted.items() if value in reasons)
    assert not leaked, leaked
