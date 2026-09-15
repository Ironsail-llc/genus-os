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
