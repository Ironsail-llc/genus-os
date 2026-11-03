"""The Helm's Secrets page: which credentials exist, where, and who reads them.

What these tests pin, and why each is a test rather than a docstring:

* **The gate is the operator's.** The list names every credential the
  instance holds and where it sits; an agent's own token must not reach it,
  and neither must a viewer, an auditor or another tenant's owner.
* **Nothing value-shaped leaves.** Not the value, not the fingerprint, not the
  vault key it was found under. A fake status row carries a sentinel in every
  one of those fields and the whole response body is searched for it, so a
  field added to the response later cannot leak one by accident.
* **Readers come from the declarations.** The units a secret's settings field
  declares, or the conservative default pair when nothing declares it — the
  same rule ``genus config`` uses, never a list kept beside it.
* **The browser keeps no copy.** ``Cache-Control: no-store``.
"""

from __future__ import annotations

import pytest

from robothor.secrets.status import SecretStatus
from robothor.settings.operator import DEFAULT_UNITS

SECRETS = "/api/secrets"
SENTINEL = "SENTINEL-NEVER-SHOWN-7f3a"


def _row(name: str, **overrides) -> SecretStatus:
    base = {
        "name": name,
        "configured": True,
        "fingerprint": f"b2:{SENTINEL}",
        "source": "vault",
        "in_env": False,
        "in_vault": True,
        "bootstrap": False,
        "updated_at": "2026-07-01T12:00:00+00:00",
        "shadowed": False,
        "shadowed_by": None,
        "env_fingerprint": f"b2:{SENTINEL}-env",
        "vault_fingerprint": f"b2:{SENTINEL}-vault",
        "vault_key": f"providers/{SENTINEL}/api_key",
        "vault_readable": True,
    }
    base.update(overrides)
    return SecretStatus(**base)


FAKE_INDEX = {
    "EXAMPLE_API_TOKEN": {
        "env": "EXAMPLE_API_TOKEN",
        "secret": True,
        "restart_units": ["robothor-engine"],
    },
    # A deprecated alias points at the same record as its primary name.
    "EXAMPLE_OLD_TOKEN": {
        "env": "EXAMPLE_NEW_TOKEN",
        "secret": True,
        "restart_units": ["robothor-bridge", "robothor-engine"],
    },
    "EXAMPLE_NEW_TOKEN": {
        "env": "EXAMPLE_NEW_TOKEN",
        "secret": True,
        "restart_units": ["robothor-bridge", "robothor-engine"],
    },
    "EXAMPLE_UNSTAMPED": {
        "env": "EXAMPLE_UNSTAMPED",
        "secret": True,
        "restart_units": None,
    },
}


@pytest.fixture
def fake_table(monkeypatch):
    """Four rows, one per source, every value-shaped field carrying the sentinel."""
    from routers import secrets

    rows = [
        _row("EXAMPLE_API_TOKEN"),
        _row(
            "EXAMPLE_OLD_TOKEN",
            source="env",
            in_env=True,
            in_vault=True,
            shadowed=True,
            shadowed_by="the vault",
        ),
        _row(
            "EXAMPLE_UNSTAMPED",
            source="env",
            in_env=True,
            in_vault=False,
            updated_at=None,
            vault_key=None,
        ),
        _row(
            "UNDECLARED_VENDOR_TOKEN",
            configured=False,
            fingerprint=None,
            source="missing",
            in_env=False,
            in_vault=False,
            updated_at=None,
            vault_key=None,
        ),
    ]
    calls: list[dict] = []

    def _table(**kwargs):
        calls.append(kwargs)
        return rows

    monkeypatch.setattr(secrets, "status_table", _table)
    monkeypatch.setattr(secrets, "field_index", lambda: FAKE_INDEX)
    return calls


def _by_name(body):
    return {row["name"]: row for row in body["secrets"]}


# ── the gate ─────────────────────────────────────────────────────────────────


def test_a_service_token_is_refused(controls_client_as_service, fake_table):
    """An agent must not list the credentials it runs with."""
    assert controls_client_as_service.get(SECRETS).status_code == 403
    assert fake_table == [], "the gate must refuse before the table is read"


def test_a_viewer_is_refused(controls_client_as_viewer, fake_table):
    assert controls_client_as_viewer.get(SECRETS).status_code == 403
    assert fake_table == []


def test_an_auditor_is_refused(controls_client_as_auditor, fake_table):
    assert controls_client_as_auditor.get(SECRETS).status_code == 403


def test_another_tenants_owner_is_refused(controls_client_as_other_tenant_owner, fake_table):
    assert controls_client_as_other_tenant_owner.get(SECRETS).status_code == 403


def test_a_refusal_is_not_cached_either(controls_client_as_viewer, fake_table):
    res = controls_client_as_viewer.get(SECRETS)
    assert res.status_code == 403
    assert res.headers.get("cache-control") == "no-store"


def test_an_admin_is_an_operator(controls_client_as_admin, fake_table):
    assert controls_client_as_admin.get(SECRETS).status_code == 200


# ── the shape ────────────────────────────────────────────────────────────────


def test_each_row_has_exactly_the_public_fields(controls_client_as_operator, fake_table):
    res = controls_client_as_operator.get(SECRETS)
    assert res.status_code == 200
    rows = res.json()["secrets"]
    assert [r["name"] for r in rows] == [
        "EXAMPLE_API_TOKEN",
        "EXAMPLE_OLD_TOKEN",
        "EXAMPLE_UNSTAMPED",
        "UNDECLARED_VENDOR_TOKEN",
    ]
    for row in rows:
        assert set(row) == {
            "name",
            "configured",
            "source",
            "updated_at",
            "readers",
            "shadowed",
            "shadowed_by",
            "env_name",
            "vault_readable",
            "removable",
        }


def test_source_names_the_stores_holding_it(controls_client_as_operator, fake_table):
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert rows["EXAMPLE_API_TOKEN"]["source"] == "vault"
    assert rows["EXAMPLE_OLD_TOKEN"]["source"] == "both"
    assert rows["EXAMPLE_UNSTAMPED"]["source"] == "env"
    assert rows["UNDECLARED_VENDOR_TOKEN"]["source"] == "none"


def test_configured_shadow_and_timestamp_pass_through(controls_client_as_operator, fake_table):
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert rows["EXAMPLE_API_TOKEN"]["configured"] is True
    assert rows["EXAMPLE_API_TOKEN"]["updated_at"] == "2026-07-01T12:00:00+00:00"
    assert rows["EXAMPLE_OLD_TOKEN"]["shadowed"] is True
    assert rows["EXAMPLE_OLD_TOKEN"]["shadowed_by"] == "the vault"
    assert rows["EXAMPLE_UNSTAMPED"]["updated_at"] is None
    assert rows["UNDECLARED_VENDOR_TOKEN"]["configured"] is False


def test_env_name_is_the_declared_primary_name(controls_client_as_operator, fake_table):
    """An alias row says which variable the platform actually declares."""
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert rows["EXAMPLE_OLD_TOKEN"]["env_name"] == "EXAMPLE_NEW_TOKEN"
    assert rows["EXAMPLE_API_TOKEN"]["env_name"] == "EXAMPLE_API_TOKEN"
    assert rows["UNDECLARED_VENDOR_TOKEN"]["env_name"] == "UNDECLARED_VENDOR_TOKEN"


def test_the_table_is_read_for_the_platform_tenant(controls_client_as_operator, fake_table):
    from routers._operator import PLATFORM_TENANT

    controls_client_as_operator.get(SECRETS)
    assert fake_table == [{"tenant_id": PLATFORM_TENANT}]


# ── never a value ────────────────────────────────────────────────────────────


def test_no_value_fingerprint_or_vault_key_reaches_the_body(
    controls_client_as_operator, fake_table
):
    res = controls_client_as_operator.get(SECRETS)
    assert res.status_code == 200
    assert SENTINEL not in res.text
    assert "fingerprint" not in res.text
    assert "vault_key" not in res.text


def test_the_browser_keeps_no_copy(controls_client_as_operator, fake_table):
    res = controls_client_as_operator.get(SECRETS)
    assert res.headers.get("cache-control") == "no-store"


# ── readers ──────────────────────────────────────────────────────────────────


def test_readers_are_the_units_the_declaration_names(controls_client_as_operator, fake_table):
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert rows["EXAMPLE_API_TOKEN"]["readers"] == ["robothor-engine"]
    assert rows["EXAMPLE_OLD_TOKEN"]["readers"] == ["robothor-bridge", "robothor-engine"]


def test_an_unstamped_or_undeclared_secret_gets_the_default_units(
    controls_client_as_operator, fake_table
):
    """Naming too few readers is how a rotation reports done and is not."""
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert rows["EXAMPLE_UNSTAMPED"]["readers"] == list(DEFAULT_UNITS)
    assert rows["UNDECLARED_VENDOR_TOKEN"]["readers"] == list(DEFAULT_UNITS)


def test_readers_agree_with_genus_config(controls_client_as_operator, monkeypatch):
    """With the real registry: one declared secret's readers are ``units_for`` its row."""
    from routers import secrets

    from robothor.settings.operator import units_for
    from robothor.settings.registry import field_index

    index = field_index()
    name = next(n for n, row in sorted(index.items()) if row["secret"] and row["env"] == n)
    monkeypatch.setattr(secrets, "status_table", lambda **_: [_row(name)])
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert rows[name]["readers"] == list(units_for(index[name]))


# ── an unreadable vault ──────────────────────────────────────────────────────


def test_an_unreadable_vault_is_said_not_hidden(controls_client_as_operator, monkeypatch):
    """Without the flag, every vault-only credential reads as "not configured"."""
    from routers import secrets

    rows = [
        _row(
            "EXAMPLE_API_TOKEN",
            configured=False,
            source="unavailable",
            in_vault=False,
            vault_readable=False,
        )
    ]
    monkeypatch.setattr(secrets, "status_table", lambda **_: rows)
    monkeypatch.setattr(secrets, "field_index", lambda: FAKE_INDEX)
    res = controls_client_as_operator.get(SECRETS)
    row = _by_name(res.json())["EXAMPLE_API_TOKEN"]
    assert row["vault_readable"] is False
    assert SENTINEL not in res.text


def test_a_readable_vault_says_so(controls_client_as_operator, fake_table):
    rows = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert all(row["vault_readable"] is True for row in rows.values())


# ── removing a vault row ─────────────────────────────────────────────────────
#
# ``DELETE /api/secrets/{name}`` takes the ENV name the list shows, never a
# vault key: the list does not send vault keys, so the page has none to send
# back. The route finds the row the accessor would serve and deletes that.


@pytest.fixture
def deletable(monkeypatch):
    from routers import secrets

    rows = {"EXAMPLE_API_TOKEN": "providers/example/api_key"}
    deleted: list[str] = []
    audits: list[tuple[str, dict]] = []
    reloads: list[str] = []

    def _status(name, **kwargs):
        vault_key = rows.get(name)
        return _row(
            name,
            configured=vault_key is not None,
            in_vault=vault_key is not None,
            source="vault" if vault_key else "missing",
            vault_key=vault_key,
        )

    def _delete(key, **kwargs):
        deleted.append(key)
        for name, vk in list(rows.items()):
            if vk == key:
                rows.pop(name)
        return True

    monkeypatch.setattr(
        secrets,
        "status_table",
        lambda **kw: (
            [_status(name) for name in sorted(rows)]
            + [_row("AUTH_JWT_SIGNING_KEY", vault_key="auth/jwt_signing_key")]
        ),
    )
    monkeypatch.setattr(secrets, "_vault_delete", _delete)
    monkeypatch.setattr(secrets, "audited", lambda request, event, **kw: audits.append((event, kw)))
    monkeypatch.setattr(secrets, "_reload_engine", lambda: reloads.append("reload"))
    return {"deleted": deleted, "audits": audits, "reloads": reloads}


def test_an_operator_removes_a_vault_row(controls_client_as_operator, deletable):
    res = controls_client_as_operator.delete(f"{SECRETS}/EXAMPLE_API_TOKEN")
    assert res.status_code == 200, res.text
    assert res.json()["deleted"] is True
    assert deletable["deleted"] == ["providers/example/api_key"]
    assert [event for event, _ in deletable["audits"]] == ["secret.delete"]
    assert deletable["reloads"] == ["reload"]
    assert res.headers.get("cache-control") == "no-store"
    assert "providers/example" not in res.text, "the vault key must not leave"


def test_an_admin_may_remove_one_too(controls_client_as_admin, deletable):
    assert controls_client_as_admin.delete(f"{SECRETS}/EXAMPLE_API_TOKEN").status_code == 200


def test_a_viewer_cannot_remove_a_secret(controls_client_as_viewer, deletable):
    assert controls_client_as_viewer.delete(f"{SECRETS}/EXAMPLE_API_TOKEN").status_code == 403
    assert deletable["deleted"] == []


def test_a_service_token_cannot_remove_a_secret(controls_client_as_service, deletable):
    assert controls_client_as_service.delete(f"{SECRETS}/EXAMPLE_API_TOKEN").status_code == 403
    assert deletable["deleted"] == []


def test_a_name_with_no_vault_row_is_not_found(controls_client_as_operator, deletable):
    res = controls_client_as_operator.delete(f"{SECRETS}/NOT_IN_THE_VAULT")
    assert res.status_code == 404
    assert deletable["deleted"] == []


def test_a_bootstrap_credential_is_refused(controls_client_as_operator, deletable):
    res = controls_client_as_operator.delete(f"{SECRETS}/ROBOTHOR_DB_PASSWORD")
    assert res.status_code == 409
    assert deletable["deleted"] == []


def test_removable_is_true_only_for_a_vault_row_that_is_not_bootstrap(
    controls_client_as_operator, monkeypatch
):
    """The page shows Remove only where the route will act on it."""
    from routers import secrets

    rows = [
        _row("EXAMPLE_API_TOKEN"),
        _row("EXAMPLE_ENV_ONLY", source="env", in_env=True, in_vault=False, vault_key=None),
        _row("AUTH_JWT_SIGNING_KEY", vault_key="auth/jwt_signing_key"),
        _row("ROBOTHOR_DB_PASSWORD", bootstrap=True, vault_key="database/password"),
    ]
    monkeypatch.setattr(secrets, "status_table", lambda **_: rows)
    monkeypatch.setattr(secrets, "field_index", lambda: FAKE_INDEX)
    listed = _by_name(controls_client_as_operator.get(SECRETS).json())
    assert listed["EXAMPLE_API_TOKEN"]["removable"] is True
    assert listed["EXAMPLE_ENV_ONLY"]["removable"] is False
    assert listed["AUTH_JWT_SIGNING_KEY"]["removable"] is False
    assert listed["ROBOTHOR_DB_PASSWORD"]["removable"] is False


def test_a_row_nothing_reads_is_removed_by_the_key_the_list_found(
    controls_client_as_operator, monkeypatch
):
    """A row nothing reads is listed under its exported name with the vault key
    the table found it at. Looking the name up again without that key found
    nothing and answered 404, so Remove failed on exactly those rows."""
    from routers import secrets

    deleted: list[str] = []
    monkeypatch.setattr(
        secrets,
        "status_table",
        lambda **_: [_row("PROVIDERS_EXAMPLE_TOKEN", vault_key="providers/example/token")],
    )
    monkeypatch.setattr(secrets, "_vault_delete", lambda key, **kw: deleted.append(key) or True)
    monkeypatch.setattr(secrets, "audited", lambda *a, **kw: None)
    monkeypatch.setattr(secrets, "_reload_engine", lambda: None)
    res = controls_client_as_operator.delete(f"{SECRETS}/PROVIDERS_EXAMPLE_TOKEN")
    assert res.status_code == 200, res.text
    assert deleted == ["providers/example/token"]


def test_the_session_signing_key_is_refused(controls_client_as_operator, deletable):
    res = controls_client_as_operator.delete(f"{SECRETS}/AUTH_JWT_SIGNING_KEY")
    assert res.status_code == 409
    assert deletable["deleted"] == []
