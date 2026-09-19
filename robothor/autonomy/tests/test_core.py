"""Authority and ciphertext boundaries, with no live services."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from robothor.autonomy.crypto import open_resource, seal_resource
from robothor.autonomy.models import Delegation, PaymentCard, Scope, WebOperation


def scope(owner="alice"):
    return Scope(tenant_id="test", owner_id=owner)


def grant(**changes):
    values = {
        "agent_ids": {"main"},
        "origins": {"https://shop.example"},
        "actions": {"purchase", "account", "application", "login"},
        "expires_at": datetime.now(UTC) + timedelta(days=30),
        "currency": "USD",
        "per_purchase_minor": 10000,
        "monthly_minor": 20000,
        "recurring_minor": 5000,
        "annual_minor": 60000,
    }
    return Delegation(**(values | changes))


def operation(**changes):
    return WebOperation(
        **(
            {
                "origin": "https://shop.example",
                "action": "purchase",
                "amount_minor": 5000,
                "currency": "USD",
                "idempotency_key": "order-1",
                "purpose": "Requested purchase",
            }
            | changes
        )
    )


def test_ciphertext_is_bound_to_owner_record_and_key_version():
    keys = {"v1": b"a" * 32, "v2": b"b" * 32}
    blob = seal_resource("private value", keys, "v1", scope(), "r1")
    assert b"private value" not in blob
    assert open_resource(blob, keys, scope(), "r1") == "private value"
    for identity, record in [(scope("bob"), "r1"), (scope(), "r2")]:
        with pytest.raises(InvalidTag):
            open_resource(blob, keys, identity, record)
    rotated = seal_resource(open_resource(blob, keys, scope(), "r1"), keys, "v2", scope(), "r1")
    assert open_resource(rotated, {"v2": keys["v2"]}, scope(), "r1") == "private value"


def test_standing_grant_allows_purchase_without_another_approval():
    assert grant().decision(operation(), agent_id="main", used_minor=15000) == "allow"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"enabled": False}, "grant_disabled"),
        ({"agent_ids": {"worker"}}, "agent_not_allowed"),
        ({"origins": {"https://other.example"}}, "origin_not_allowed"),
        ({"per_purchase_minor": 4999}, "purchase_limit"),
        ({"monthly_minor": 4999}, "monthly_limit"),
        ({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, "grant_expired"),
    ],
)
def test_denies_outside_authority(changes, reason):
    assert grant(**changes).decision(operation(), agent_id="main", used_minor=0) == reason


def test_recurring_commitment_requires_both_limits():
    op = operation(recurring_minor=4000, annual_commitment_minor=48000)
    assert grant().decision(op, agent_id="main", used_minor=0) == "allow"
    assert grant(annual_minor=47000).decision(op, agent_id="main", used_minor=0) == "annual_limit"


@pytest.mark.parametrize(
    "url",
    [
        "http://shop.example",
        "https://shop.example.evil.test/path",
        "https://user:pass@shop.example",
        "https://shop.example/path",
    ],
)
def test_origin_is_not_a_loose_url_prefix(url):
    if url == "https://shop.example.evil.test/path":
        with pytest.raises(ValidationError):
            operation(origin=url)
    else:
        with pytest.raises(ValidationError):
            operation(origin=url)


def test_card_never_accepts_a_persisted_verification_code():
    values = {
        "number": "4242424242424242",
        "expiry_month": 12,
        "expiry_year": 2030,
        "name": "Alice Example",
    }
    card = PaymentCard(**values)
    assert "4242424242424242" not in repr(card)
    assert "4242424242424242" not in card.model_dump_json()
    with pytest.raises(ValidationError):
        PaymentCard(**values, cvv="123")


def test_money_is_integral_and_nonnegative():
    for amount in (-1, 1.5, True):
        with pytest.raises(ValidationError):
            operation(amount_minor=amount)
