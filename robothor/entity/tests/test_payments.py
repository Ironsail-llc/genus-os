from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from robothor.entity.audit import REDACTED, redact_for_audit
from robothor.entity.payments import (
    ClientPaymentMethodReference,
    OperationalVirtualCardReference,
)


def test_client_payment_method_accepts_only_provider_token_reference() -> None:
    reference = ClientPaymentMethodReference(
        tenant_id="tenant-1",
        organization_id="org-1",
        client_id="client-1",
        payment_method_id="method-1",
        provider="stripe",
        provider_payment_method_token="pm_external_A1b2c3d4",
    )

    assert reference.reference_type == "client_payment_method"
    assert reference.to_audit_dict()["token_fingerprint"]
    assert "pm_external_A1b2c3d4" not in repr(reference)
    assert "pm_external_A1b2c3d4" not in reference.model_dump_json()


@pytest.mark.parametrize("forbidden_field", ["pan", "card_number", "cvv", "cvc"])
def test_client_payment_method_rejects_raw_card_fields(forbidden_field: str) -> None:
    data = {
        "tenant_id": "tenant-1",
        "organization_id": "org-1",
        "client_id": "client-1",
        "payment_method_id": "method-1",
        "provider": "stripe",
        "provider_payment_method_token": "pm_external_A1b2c3d4",
        forbidden_field: "4242424242424242"
        if "card" in forbidden_field or forbidden_field == "pan"
        else "123",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted") as exc_info:
        ClientPaymentMethodReference.model_validate(data)
    assert str(data[forbidden_field]) not in str(exc_info.value)


@pytest.mark.parametrize(
    "raw_value",
    [
        "4242424242424242",
        "4242-4242-4242-4242",
        "4242 4242 4242 4242",
        "tok_4242424242424242",
        "123",
    ],
)
def test_provider_reference_rejects_pan_and_short_cvv(raw_value: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        OperationalVirtualCardReference(
            tenant_id="tenant-1",
            organization_id="org-1",
            virtual_card_id="card-1",
            provider="stripe",
            provider_virtual_card_token=raw_value,
        )
    assert raw_value not in str(exc_info.value)


def test_virtual_card_secret_is_redacted_from_repr_json_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "vc_provider_SensitiveRef987"
    reference = OperationalVirtualCardReference(
        tenant_id="tenant-1",
        organization_id="org-1",
        virtual_card_id="card-1",
        provider="ramp",
        provider_virtual_card_token=token,
        last_four="4242",
        brand="visa",
    )

    caplog.set_level(logging.INFO)
    logging.getLogger("entity-test").info("virtual card reference: %r", reference)

    assert token not in repr(reference)
    assert token not in reference.model_dump_json()
    assert token not in caplog.text
    assert reference.to_audit_dict()["card_display"] == "****4242"


def test_recursive_audit_redaction_handles_camel_case_and_embedded_pan() -> None:
    redacted = redact_for_audit(
        {
            "providerReference": "vc_provider_SensitiveRef987",
            "cvv": "123",
            "message": "do not log 4242 4242 4242 4242 here",
            "safe": {"amount": "12.50"},
        }
    )

    assert isinstance(redacted, dict)
    assert redacted["providerReference"] == REDACTED
    assert redacted["cvv"] == REDACTED
    assert redacted["message"] == REDACTED
    assert redacted["safe"] == {"amount": "12.50"}
