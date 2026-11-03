"""SSO domain claims on a tenant: validation and the one-claimant rule (DB mocked).

A tenant claims email domains in ``settings.sso_domains`` so that a brand-new
SSO user from that domain is provisioned into it. Two tenants claiming one
domain would make that routing ambiguous, so saving such a list is refused, as
is anything that is not a plain lower-case DNS name.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.crm import dal
from robothor.crm.tenant_domains import (
    InvalidSsoDomainError,
    SsoDomainConflictError,
    normalize_sso_domains,
    sso_domains_of,
)

# ─── normalize_sso_domains ───────────────────────────────────────────────


def test_normalize_lowercases_strips_and_dedupes() -> None:
    assert normalize_sso_domains([" Example.com ", "other.example", "EXAMPLE.COM"]) == [
        "example.com",
        "other.example",
    ]


def test_normalize_accepts_an_empty_list() -> None:
    assert normalize_sso_domains([]) == []


@pytest.mark.parametrize(
    "bad",
    [
        "not a domain",
        "http://example.com",
        "@example.com",
        "alice@example.com",
        "example",
        "-example.com",
        "example..com",
        "",
        "*.example.com",
    ],
)
def test_normalize_rejects_a_malformed_domain(bad: str) -> None:
    with pytest.raises(InvalidSsoDomainError):
        normalize_sso_domains([bad])


@pytest.mark.parametrize("bad", ["example.com", {"example.com": True}, [1], [None]])
def test_normalize_rejects_anything_but_a_list_of_strings(bad: object) -> None:
    with pytest.raises(InvalidSsoDomainError):
        normalize_sso_domains(bad)


def test_sso_domains_of_reads_the_settings_of_a_tenant_dict() -> None:
    assert sso_domains_of({"settings": {"sso_domains": ["example.com"]}}) == ["example.com"]
    assert sso_domains_of({"settings": {}}) == []
    assert sso_domains_of({"settings": None}) == []
    # A hand-edited, malformed value never crashes a reader.
    assert sso_domains_of({"settings": {"sso_domains": "example.com"}}) == []


# ─── update_tenant ───────────────────────────────────────────────────────


def _mock_conn(other_tenants: list[tuple[str, object]]) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.fetchall.return_value = other_tenants
    cur.rowcount = 1
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    return conn, cur


def _executed(cur: MagicMock) -> str:
    return " ".join(str(c.args[0]) for c in cur.execute.call_args_list)


def test_update_tenant_refuses_a_domain_another_tenant_claims() -> None:
    conn, cur = _mock_conn([("acme-parent", {"sso_domains": ["example.com"]})])
    with (
        patch("robothor.crm.dal.get_connection", return_value=conn),
        pytest.raises(SsoDomainConflictError) as exc,
    ):
        dal.update_tenant("acme-child-a", settings={"sso_domains": ["Example.com"]})
    assert exc.value.domain == "example.com"
    assert exc.value.tenant_id == "acme-parent"
    assert "UPDATE crm_tenants" not in _executed(cur)
    conn.commit.assert_not_called()


def test_update_tenant_refuses_a_malformed_domain_before_touching_the_db() -> None:
    conn, cur = _mock_conn([])
    with (
        patch("robothor.crm.dal.get_connection", return_value=conn),
        pytest.raises(InvalidSsoDomainError),
    ):
        dal.update_tenant("acme-child-a", settings={"sso_domains": ["not a domain"]})
    assert "UPDATE crm_tenants" not in _executed(cur)


def test_update_tenant_saves_normalized_domains_when_unclaimed() -> None:
    conn, cur = _mock_conn([("acme-parent", {"sso_domains": ["other.example"]})])
    with patch("robothor.crm.dal.get_connection", return_value=conn):
        ok = dal.update_tenant("acme-child-a", settings={"sso_domains": ["Example.com"]})
    assert ok is True
    update = next(c for c in cur.execute.call_args_list if "UPDATE crm_tenants" in c.args[0])
    assert '"sso_domains": ["example.com"]' in update.args[1][0]
    conn.commit.assert_called()


def test_update_tenant_without_sso_domains_does_not_check_claims() -> None:
    conn, cur = _mock_conn([])
    with patch("robothor.crm.dal.get_connection", return_value=conn):
        assert dal.update_tenant("acme-child-a", display_name="Acme Child A") is True
    assert "LOCK TABLE" not in _executed(cur)
