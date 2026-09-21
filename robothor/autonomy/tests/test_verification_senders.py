"""Additional mail senders are explicit, destination-bound owner authority."""

import base64
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
from pydantic import ValidationError

from robothor.autonomy.models import Delegation
from robothor.autonomy.tests.test_store import policy
from robothor.autonomy.tests.test_verification import email
from robothor.autonomy.verification import extract_verification


def external_message(domain="mail.provider.example"):
    message = email()
    message["payload"]["headers"][0]["value"] = "verify@" + domain
    message["payload"]["headers"][-1]["value"] = (
        "mx.google.com; dmarc=pass header.from=" + domain + ";"
    )
    return message


def extract(message, senders=frozenset()):
    return extract_verification(
        message,
        recipient="alice@example.com",
        destination="https://shop.example",
        after=int(datetime.now(UTC).timestamp()) - 60,
        mode="code",
        sender_domains=senders,
    )


def test_external_sender_requires_exact_explicit_authority_and_authentication():
    assert extract(external_message()) is None
    allowed = frozenset({"mail.provider.example"})
    assert extract(external_message(), allowed) == "123456"
    assert extract(external_message("evil.mail.provider.example"), allowed) is None
    message = external_message()
    message["payload"]["headers"][-1]["value"] = (
        "mx.google.com; dmarc=fail header.from=mail.provider.example;"
    )
    assert extract(message, allowed) is None


def test_additional_sender_does_not_authorize_foreign_verification_link():
    import base64

    message = external_message()
    for target, accepted in [
        ("https://shop.example/verify?t=canary", True),
        ("https://mail.provider.example/verify?t=canary", False),
    ]:
        message["payload"]["body"]["data"] = base64.urlsafe_b64encode(target.encode()).decode()
        result = extract_verification(
            message,
            recipient="alice@example.com",
            destination="https://shop.example",
            after=int(datetime.now(UTC).timestamp()) - 60,
            mode="link",
            sender_domains=frozenset({"mail.provider.example"}),
        )
        assert result == (target if accepted else None)


def test_sender_mapping_is_normalized_and_persisted_per_destination(store, identity):
    grant = Delegation.model_validate(
        {
            **policy().model_dump(),
            "verification_senders": {"https://SHOP.example/": ["Mail.Provider.Example"]},
        }
    )
    assert grant.verification_senders == {
        "https://shop.example": frozenset({"mail.provider.example"})
    }
    saved = store.create_grant(identity, grant)
    restored = Delegation.model_validate(
        next(row["policy"] for row in store.grants(identity) if row["id"] == saved["id"])
    )
    assert restored.verification_senders == grant.verification_senders
    assert restored.verification_senders.get("https://other.example", frozenset()) == frozenset()


def test_operation_mail_cutoff_does_not_round_past_immediate_message(store, identity):
    from robothor.autonomy.tests.test_store import proposal

    grant = store.create_grant(identity, policy())
    op = store.reserve(identity, grant["id"], "main", proposal())
    stamp = datetime(2026, 9, 1, 12, 0, 0, 900000, tzinfo=UTC)
    with store.transaction() as cur:
        cur.execute("UPDATE autonomy_operations SET created_at=%s WHERE id=%s", (stamp, op["id"]))
    assert store.operation(identity, op["id"])["created_epoch"] == int(stamp.timestamp())


@pytest.mark.parametrize(
    "domain",
    [
        "*.example.com",
        "example.com/path",
        "example.com:443",
        "x@example.com",
        "localhost",
        "127.0.0.1",
        "-bad.example",
        "example..com",
        "example.com\nfrom:evil",
    ],
)
def test_sender_authority_rejects_wildcards_addresses_urls_and_query_injection(domain):
    with pytest.raises(ValidationError):
        Delegation.model_validate(
            {
                **policy().model_dump(),
                "verification_senders": {"https://shop.example": [domain]},
            }
        )


@pytest.mark.parametrize("configured,revoked", [(False, False), (True, False), (True, True)])
async def test_mailbox_handler_uses_grant_not_agent_claims_and_rechecks_revocation(
    store, identity, monkeypatch, configured, revoked
):
    import json

    from robothor import constants
    from robothor.autonomy.models import ResourceInput
    from robothor.autonomy.tests.test_store import proposal
    from robothor.crm import dal
    from robothor.engine.tools import dispatch
    from robothor.engine.tools.handlers import autonomy, gws

    settings = store.settings(identity)
    identity = identity.model_copy(update={"owner_id": "person:alice"})
    store.configure(identity, settings)
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(autonomy, "scope_for_actor", lambda *args: identity)
    monkeypatch.setattr(constants, "DEFAULT_TENANT", identity.tenant_id)
    monkeypatch.setattr(dal, "get_owner_person", lambda *args: {"id": "alice"})
    monkeypatch.setattr(
        dispatch, "get_agent_toolset", lambda: {"gws_gmail_search", "gws_gmail_get"}
    )
    grant = store.create_grant(
        identity,
        Delegation.model_validate(
            {
                **policy().model_dump(),
                "verification_senders": {"https://shop.example": ["mail.provider.example"]}
                if configured
                else {},
            }
        ),
    )
    op = store.reserve(identity, grant["id"], "main", proposal())
    profile = store.put_resource(
        identity,
        ResourceInput(
            kind="profile", label="Contact", payload=json.dumps({"email": "alice@example.com"})
        ),
    )
    queries = []

    async def search(args, ctx):
        queries.append(args["query"])
        return {"messages": [{"id": "fixture-mail"}]}

    def fetch(*args):
        if revoked:
            store.revoke_grant(identity, grant["id"])
        return external_message()

    monkeypatch.setitem(gws.HANDLERS, "gws_gmail_search", search)
    monkeypatch.setattr(gws, "_fetch_message", fetch)
    result = await autonomy.handle(
        {
            "kind": "email_verification",
            "operation_id": op["id"],
            "profile_id": profile["id"],
            "sender_domains": ["mail.provider.example"],
        },
        dispatch.ToolContext(
            agent_id="main", user_id="actor", user_role="owner", tenant_id=identity.tenant_id
        ),
    )
    assert queries, result
    assert ("mail.provider.example" in queries[0]) == configured
    assert "123456" not in json.dumps(result)
    if configured and not revoked:
        assert "id" in result, result
        assert (
            store.consume_resource(identity, result["id"], "https://shop.example")["password"]
            == "123456"
        )
    else:
        assert "id" not in result
        assert len(store.resources(identity)) == 1


@pytest.mark.parametrize(
    "domain",
    [
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "sendgrid.net",
        "amazonses.com",
        "mailgun.org",
        "postmarkapp.com",
        "mcsv.net",
        "brevo.com",
        "em1234.sendgrid.net",
        "us-east-1.amazonses.com",
    ],
)
def test_a_shared_mail_domain_cannot_be_authorized_as_a_verification_sender(domain):
    """DMARC passes for every user of a shared domain, so it proves nothing."""
    with pytest.raises(ValidationError):
        Delegation.model_validate(
            {
                **policy().model_dump(),
                "verification_senders": {"https://shop.example": [domain]},
            }
        )


def test_a_single_tenant_subdomain_of_a_shop_is_still_authorizable():
    grant = Delegation.model_validate(
        {
            **policy().model_dump(),
            "verification_senders": {"https://shop.example": ["mail.acme-shop.example"]},
        }
    )
    assert grant.verification_senders == {
        "https://shop.example": frozenset({"mail.acme-shop.example"})
    }


@pytest.mark.parametrize("domain", ["gmail.com", "sendgrid.net"])
def test_a_shared_mail_domain_stored_before_this_rule_is_still_refused_at_match_time(domain):
    """Authority written by an earlier release is not a reason to accept it."""
    assert extract(external_message(domain), frozenset({domain})) is None


async def test_the_oldest_matching_message_wins_over_a_later_authorized_sender(
    store, identity, monkeypatch
):
    """Gmail answers newest first; a sender who replies after the site must not win."""
    import json
    from datetime import timedelta

    from robothor import constants
    from robothor.autonomy.models import ResourceInput
    from robothor.autonomy.tests.test_store import proposal
    from robothor.crm import dal
    from robothor.engine.tools import dispatch
    from robothor.engine.tools.handlers import autonomy, gws

    settings = store.settings(identity)
    identity = identity.model_copy(update={"owner_id": "person:alice"})
    store.configure(identity, settings)
    monkeypatch.setattr(autonomy, "AutonomyStore", lambda: store)
    monkeypatch.setattr(autonomy, "scope_for_actor", lambda *args: identity)
    monkeypatch.setattr(constants, "DEFAULT_TENANT", identity.tenant_id)
    monkeypatch.setattr(dal, "get_owner_person", lambda *args: {"id": "alice"})
    monkeypatch.setattr(
        dispatch, "get_agent_toolset", lambda: {"gws_gmail_search", "gws_gmail_get"}
    )
    grant = store.create_grant(
        identity,
        Delegation.model_validate(
            {
                **policy().model_dump(),
                "verification_senders": {"https://shop.example": ["mail.provider.example"]},
            }
        ),
    )
    op = store.reserve(identity, grant["id"], "main", proposal())
    profile = store.put_resource(
        identity,
        ResourceInput(
            kind="profile", label="Contact", payload=json.dumps({"email": "alice@example.com"})
        ),
    )

    def stamped(message, code, offset):
        when = datetime.now(UTC) + timedelta(seconds=offset)
        message["internalDate"] = str(int(when.timestamp() * 1000))
        for item in message["payload"]["headers"]:
            if item["name"] == "Date":
                item["value"] = format_datetime(when)
        message["payload"]["body"]["data"] = base64.urlsafe_b64encode(
            f"Your verification code is {code}".encode()
        ).decode()
        return message

    messages = {
        "later": stamped(external_message(), "999999", 20),
        "earlier": stamped(email(), "123456", 1),
    }

    async def search(args, ctx):
        # Newest first, exactly as the mailbox returns them.
        return {"messages": [{"id": "later"}, {"id": "earlier"}]}

    monkeypatch.setitem(gws.HANDLERS, "gws_gmail_search", search)
    monkeypatch.setattr(gws, "_fetch_message", lambda message_id, *args: messages[message_id])
    result = await autonomy.handle(
        {
            "kind": "email_verification",
            "operation_id": op["id"],
            "profile_id": profile["id"],
        },
        dispatch.ToolContext(
            agent_id="main", user_id="actor", user_role="owner", tenant_id=identity.tenant_id
        ),
    )
    assert "id" in result, result
    assert (
        store.consume_resource(identity, result["id"], "https://shop.example")["password"]
        == "123456"
    )
