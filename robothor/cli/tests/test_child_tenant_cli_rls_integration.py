"""Onboarding a child tenant from a root shell, with RLS enforcing as robothor_app.

What end-to-end QA hit on a real stack: every ``--tenant <child>`` command ran
its DAL work under the PROCESS's tenant (the platform tenant), so the
database's ``WITH CHECK`` refused each write into the child, and each lookup
in the child found nothing. These reproduce it against real Postgres:

- ``genus tenant create <child> --parent <root>`` wrote the tenant row and then
  failed seeding its memory blocks, leaving it half-created;
- ``genus user add --tenant <child>`` could not create the CRM person;
- ``genus user set-password --tenant <child>`` found "no user account";
- ``genus tenant move-account`` refused every account ``user add`` made,
  because the person's own email identifier counted as CRM history.
"""

from __future__ import annotations

import io
import uuid
from argparse import Namespace

import pytest

from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration


def _blocks(db_conn, tenant: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM agent_memory_blocks WHERE tenant_id = %s", (tenant,))
        return int(cur.fetchone()[0])


def _user_add(tenant: str, email: str, role: str = "member", *, sso: bool = False) -> int:
    from robothor.cli.user import cmd_user

    return cmd_user(
        Namespace(
            user_command="add",
            tenant=tenant,
            name="Alice Example",
            role=role,
            telegram_id=None,
            email=email,
            person_id=None,
            create_person=False,
            sso=sso,
        )
    )


def _grant(tenant: str, email: str) -> int:
    from robothor.cli.auth import cmd_auth

    return cmd_auth(
        Namespace(
            auth_command="grant-binding",
            tenant=tenant,
            email=email,
            ttl="15m",
            reason="",
            issuer=None,
            json_output=False,
        )
    )


# ─── D1: tenant create ───────────────────────────────────────────────────


def test_tenant_create_from_a_root_shell_seeds_the_childs_blocks(db_conn, app_role):  # noqa: F811
    from robothor.cli.tenant import cmd_tenant

    child = f"acme-child-c-{uuid.uuid4().hex[:8]}"
    args = Namespace(
        tenant_command="create", id=child, name=None, telegram_user_id=None, parent="default"
    )
    assert cmd_tenant(args) == 0
    assert _blocks(db_conn, child) > 0


def test_tenant_create_reruns_to_finish_a_half_created_tenant(db_conn, app_role):  # noqa: F811
    """The row exists but its blocks do not (the state the old failure left)."""
    from robothor.cli.tenant import cmd_tenant

    child = f"acme-child-c-{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, %s, 'default')",
            (child, child),
        )
    args = Namespace(
        tenant_command="create", id=child, name=None, telegram_user_id=None, parent="default"
    )
    assert cmd_tenant(args) == 0
    assert _blocks(db_conn, child) > 0


def test_tenant_create_with_a_telegram_user_writes_it_into_the_child(db_conn, app_role):  # noqa: F811
    from robothor.crm import dal

    child = f"acme-child-c-{uuid.uuid4().hex[:8]}"
    assert (
        dal.create_tenant_with_user(
            tenant_id=child,
            display_name=child,
            telegram_user_id=f"tg-{child}",
            user_display_name="Alice",
            parent_tenant_id="default",
        )
        == child
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id FROM tenant_users WHERE telegram_user_id = %s", (f"tg-{child}",)
        )
        assert [r[0] for r in cur.fetchall()] == [child]
    assert _blocks(db_conn, child) > 0


# ─── D2 / D3: user add, set-password ─────────────────────────────────────


def test_user_add_into_a_child_from_a_root_shell(db_conn, app_role, rls_tenants):  # noqa: F811
    child = rls_tenants["a"]
    assert _user_add(child, "alice@example.com") == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT ua.tenant_id, p.tenant_id FROM user_accounts ua "
            "JOIN crm_people p ON p.id = ua.person_id WHERE ua.email = 'alice@example.com' "
            "AND ua.tenant_id = %s",
            (child,),
        )
        assert cur.fetchall() == [(child, child)]


def test_set_password_in_a_child_from_a_root_shell(db_conn, app_role, rls_tenants, monkeypatch):  # noqa: F811
    from robothor.cli.user import cmd_user

    child = rls_tenants["a"]
    assert _user_add(child, "alice@example.com") == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("correct-horse-battery-staple\n"))
    args = Namespace(
        user_command="set-password", tenant=child, email="alice@example.com", password_stdin=True
    )
    assert cmd_user(args) == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT password_hash IS NOT NULL FROM user_accounts "
            "WHERE tenant_id = %s AND email = 'alice@example.com'",
            (child,),
        )
        assert cur.fetchone()[0] is True


# ─── D6: a plain `user add` account can be moved ─────────────────────────


def test_a_user_add_account_moves_with_its_own_identifiers(db_conn, app_role, rls_tenants):  # noqa: F811
    from robothor.auth import accounts

    a, b = rls_tenants["a"], rls_tenants["b"]
    assert _user_add(a, "alice@example.com") == 0
    [found] = [x for x in accounts.accounts_for_email("alice@example.com") if x["tenant_id"] == a]

    dry = accounts.move_account(str(found["id"]), b, actor="cli:test", dry_run=True)
    assert dry.counts["contact_identifiers"] == 1

    report = accounts.move_account(str(found["id"]), b, actor="cli:test")
    assert report.counts["contact_identifiers"] == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id FROM contact_identifiers "
            "WHERE channel = 'email' AND identifier = 'alice@example.com' AND tenant_id IN (%s, %s)",
            (a, b),
        )
        assert [r[0] for r in cur.fetchall()] == [b]


# ─── SSO onboarding from the CLI ─────────────────────────────────────────

ISSUER = "https://idp.example.com"


def _sso(tenant: str, email: str, subject: str):
    from robothor.auth import accounts

    return accounts.jit_provision(
        issuer=ISSUER, subject=subject, email=email, display_name="Alice", tenant_id=tenant
    )


def test_an_sso_user_added_from_the_cli_signs_in_to_the_child(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
    capsys,
):
    """The same shape the Helm's SSO invite makes: an active account with no
    password, armed with a one-shot grant that the first SSO sign-in spends."""
    child = rls_tenants["a"]
    email = f"alice-{uuid.uuid4().hex[:8]}@example.com"
    assert _user_add(child, email, sso=True) == 0
    out = capsys.readouterr().out
    assert f"genus auth grant-binding --email {email} --tenant {child}" in out
    assert "robothor auth" not in out
    assert _grant(child, email) == 0

    account = _sso(child, email, "sub-alice")
    assert account["tenant_id"] == child
    assert account["status"] == "active"
    assert account["idp_issuer"] == ISSUER and account["idp_subject"] == "sub-alice"
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM user_accounts WHERE email = %s", (email,))
        assert cur.fetchone()[0] == 1


def test_an_invited_account_cannot_be_armed_and_says_how(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
    capsys,
):
    child = rls_tenants["a"]
    email = f"bob-{uuid.uuid4().hex[:8]}@example.com"
    assert _user_add(child, email) == 0
    capsys.readouterr()
    assert _grant(child, email) == 1
    assert "--sso" in capsys.readouterr().err


def test_an_invited_account_without_a_grant_is_refused_at_sso(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
):
    from robothor.auth import accounts

    child = rls_tenants["a"]
    email = f"carol-{uuid.uuid4().hex[:8]}@example.com"
    assert _user_add(child, email) == 0
    with pytest.raises(accounts.AccountBindingRequiredError):
        _sso(child, email, "sub-carol")
