"""A device handoff can reuse semantic outcome detection instead of guessed text."""

import pytest
from pydantic import ValidationError

from robothor.autonomy.handoffs import Confirmation


def test_automatic_confirmation_does_not_require_guessed_future_text():
    value = Confirmation(url="https://shop.example/status")
    assert value.selector is None and value.text is None


def test_specific_confirmation_requires_both_fields_and_a_specific_element():
    for fields in ({"selector": "#done"}, {"text": "Order confirmed"}):
        with pytest.raises(ValidationError):
            Confirmation(url="https://shop.example/status", **fields)
    assert (
        Confirmation(
            url="https://shop.example/status", selector="#done", text="Order confirmed"
        ).selector
        == "#done"
    )


def test_new_handoff_refuses_a_whole_page_guess(store, identity):
    from robothor.autonomy.handoffs import HandoffStore
    from robothor.autonomy.tests.test_handoffs import pending, request

    op = pending(store, identity)
    with pytest.raises(PermissionError, match="use_automatic_or_specific_confirmation"):
        HandoffStore(store).create(
            identity,
            op["id"],
            "main",
            request(
                confirmation={
                    "url": "https://shop.example/status",
                    "selector": "body",
                    "text": "Approved",
                }
            ),
        )
