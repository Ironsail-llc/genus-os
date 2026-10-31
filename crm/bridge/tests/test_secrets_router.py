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
