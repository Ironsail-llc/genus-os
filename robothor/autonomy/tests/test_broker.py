"""Broker controls real effects; model-facing outputs carry evidence only."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan, FieldBinding
from robothor.autonomy.models import Scope


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(
        "robothor.autonomy.terms_capture.capture_terms", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("robothor.autonomy.preflight.validate_plan", AsyncMock(return_value=[]))
    store = MagicMock()
    store.operation.return_value = {
        "state": "reserved",
        "agent_id": "main",
        "proposal": {
            "origin": "https://shop.example",
            "action": "account",
            "amount_minor": 0,
            "currency": "USD",
            "purpose": "Create requested account",
            "idempotency_key": "account-1",
            "recurring_minor": 0,
            "annual_commitment_minor": 0,
        },
    }
    store.consume_resource.return_value = {"username": "alice", "password": "private-password"}
    page = MagicMock(url="https://shop.example/signup")
    page.goto = AsyncMock()
    page.route = AsyncMock()
    page.wait_for_url = AsyncMock()
    page.locator.return_value = MagicMock()
    locator = page.locator.return_value
    locator.count = AsyncMock(return_value=1)
    locator.is_visible = AsyncMock(return_value=True)
    locator.fill = AsyncMock()
    locator.click = AsyncMock()
    locator.wait_for = AsyncMock()
    locator.inner_text = AsyncMock(return_value="Account created")
    confirmation = MagicMock()
    confirmation.is_visible = AsyncMock(side_effect=[False, False, True])
    confirmation.count = AsyncMock(return_value=1)
    confirmation.wait_for = AsyncMock()
    confirmation.inner_text = AsyncMock(return_value="Account created")
    page.locator.side_effect = lambda selector: (
        confirmation if selector == "#confirmation" else locator
    )
    page.context.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
    return store, page


def plan(**changes):
    return ExecutionPlan(
        **(
            {
                "url": "https://shop.example/signup",
                "submit_selector": "#submit",
                "success_selector": "#confirmation",
                "success_text": "Account created",
                "fields": [
                    FieldBinding(
                        selector="#password",
                        resource_id="11111111-1111-1111-1111-111111111111",
                        field="password",
                        kind="credential",
                    )
                ],
            }
            | changes
        )
    )


async def test_reference_filled_only_inside_broker_and_output_has_no_secret(setup):
    store, page = setup
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"), "op1", "main", plan(), page
    )
    page.locator.return_value.fill.assert_awaited_once_with("private-password", timeout=15000)
    assert "private-password" not in str(result)
    assert result["state"] == "completed"
    store.begin_submit.assert_called_once()
    assert store.finish.call_args.args[2] == "completed"


async def test_redirect_to_other_origin_never_receives_credential(setup):
    store, page = setup
    page.url = "https://evil.example/signup"
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"), "op1", "main", plan(), page
    )
    assert result["state"] == "reserved"
    store.consume_resource.assert_not_called()
    page.locator.return_value.fill.assert_not_awaited()


async def test_unknown_submit_outcome_is_reconciled_and_error_is_not_echoed(setup):
    store, page = setup
    page.locator.return_value.click.side_effect = TimeoutError(
        "private-password in a browser error"
    )
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"), "op1", "main", plan(), page
    )
    assert result["state"] == "reconciling"
    assert "private-password" not in str(result)


async def test_revoked_grant_does_not_fill_or_submit(setup):
    store, page = setup
    store.begin_submit.side_effect = PermissionError("grant_revoked")
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"), "op1", "main", plan(), page
    )
    page.locator.return_value.fill.assert_not_awaited()
    page.locator.return_value.click.assert_not_awaited()
    assert result["state"] == "reserved"


async def test_mismatched_visible_price_never_submits(setup):
    store, page = setup
    store.operation.return_value["proposal"].update(action="purchase", amount_minor=500)
    page.locator.return_value.inner_text.return_value = "$6.00 USD"
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"),
        "op1",
        "main",
        plan(amount_selector="#total"),
        page,
    )
    assert result["state"] == "reserved"
    page.locator.return_value.click.assert_not_awaited()


async def test_already_submitting_operation_does_not_even_navigate(setup):
    store, page = setup
    store.operation.return_value["state"] = "submitting"
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"), "op1", "main", plan(), page
    )
    page.goto.assert_not_awaited()
    assert result["state"] == "reconciling"


async def test_payment_never_persists_merchant_browser_storage(setup):
    store, page = setup
    store.operation.return_value["proposal"].update(action="purchase", amount_minor=500)
    page.locator.return_value.inner_text.return_value = "$5.00 USD"
    page.context.storage_state.return_value = {
        "cookies": [],
        "origins": [
            {"origin": "https://shop.example", "localStorage": [{"name": "code", "value": "739"}]}
        ],
    }
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"),
        "op1",
        "main",
        plan(amount_selector="#total"),
        page,
    )
    assert result["state"] == "completed"
    page.context.storage_state.assert_not_awaited()
    store.put_resource.assert_not_called()


async def test_zero_amount_proposal_still_requires_a_visible_zero_checkout_total(setup):
    store, page = setup
    store.operation.return_value["proposal"].update(action="purchase", amount_minor=0)
    page.locator.return_value.inner_text.return_value = "$5.00 USD"
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"),
        "op1",
        "main",
        plan(amount_selector="#total"),
        page,
    )
    assert result["state"] == "reserved"
    page.locator.return_value.click.assert_not_awaited()


@pytest.mark.parametrize(
    "interval,date_text,error",
    [
        ("Monthly", "October 31, 2026", None),
        ("Yearly", "October 31, 2026", "recurrence_changed"),
        ("Monthly", "November 1, 2026", "renewal_date_changed"),
    ],
)
async def test_recurring_terms_are_verified_against_visible_merchant_terms(
    interval, date_text, error
):
    from robothor.autonomy.models import WebOperation

    proposal = WebOperation(
        origin="https://shop.example",
        action="subscription",
        purpose="Join membership",
        idempotency_key="membership-1",
        amount_minor=0,
        recurring_minor=600,
        annual_commitment_minor=7200,
        recurrence={"interval_months": 1, "next_charge_on": "2026-10-31"},
    )
    execution = plan(
        fields=[],
        amount_selector="#total",
        recurring_selector="#recurring",
        annual_selector="#annual",
        recurrence_interval_selector="#interval",
        next_charge_selector="#next",
    )
    values = {
        "#total": "$0.00",
        "#recurring": "$6.00",
        "#annual": "$72.00",
        "#interval": interval,
        "#next": date_text,
    }
    page = MagicMock()

    def locator(selector):
        return MagicMock(
            count=AsyncMock(return_value=1),
            is_visible=AsyncMock(return_value=True),
            inner_text=AsyncMock(return_value=values[selector]),
        )

    page.locator.side_effect = locator
    broker = BrowserBroker(MagicMock())
    if error:
        with pytest.raises(ValueError, match=error):
            await broker._prices(page, proposal, execution)
    else:
        await broker._prices(page, proposal, execution)


@pytest.mark.parametrize(
    "saved_origin,frame_origin,allowed,accepted",
    [
        ("https://shop.example", "https://payments.example", {"https://payments.example"}, False),
        (
            "https://payments.example",
            "https://payments.example",
            {"https://payments.example"},
            True,
        ),
        ("https://shop.example", "https://shop.example", set(), True),
    ],
)
async def test_frame_discovery_and_credential_destination_rules_agree(
    saved_origin, frame_origin, allowed, accepted
):
    store = MagicMock()

    def consume(scope, resource, destination, **kwargs):
        if destination != saved_origin:
            raise PermissionError("resource_not_authorized")
        return {"password": "private"}

    store.consume_resource.side_effect = consume
    locator = MagicMock(
        count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), fill=AsyncMock()
    )
    frame = MagicMock(url=frame_origin)
    frame.locator.return_value = locator
    element = MagicMock(content_frame=AsyncMock(return_value=frame))
    page = MagicMock(url="https://shop.example/signup")
    page.route = AsyncMock()
    page.locator.return_value.element_handle = AsyncMock(return_value=element)
    binding = FieldBinding(
        selector="#password",
        resource_id="11111111-1111-1111-1111-111111111111",
        kind="credential",
        field="password",
        frame_selector="#frame",
        frame_origin=frame_origin,
    )
    broker = BrowserBroker(store)
    if accepted:
        await broker._field(
            Scope(tenant_id="test", owner_id="alice"),
            page,
            binding,
            "https://shop.example",
            frozenset(allowed),
        )
        locator.fill.assert_awaited_once_with("private", timeout=15000)
    else:
        with pytest.raises(PermissionError, match="resource_not_authorized"):
            await broker._field(
                Scope(tenant_id="test", owner_id="alice"),
                page,
                binding,
                "https://shop.example",
                frozenset(allowed),
            )
        locator.fill.assert_not_awaited()


def card_binding(selector="#card-number"):
    return FieldBinding(
        selector=selector,
        resource_id="22222222-2222-2222-2222-222222222222",
        field="number",
        kind="payment_card",
    )


@pytest.mark.parametrize("action,accepted", [("account", False), ("purchase", True)])
async def test_stored_card_is_never_typed_into_a_non_payment_form(setup, action, accepted):
    store, page = setup
    store.consume_resource.return_value = {"number": "4242424242424242"}
    store.operation.return_value["proposal"].update(
        action=action, amount_minor=500 if accepted else 0
    )
    page.locator.return_value.inner_text.return_value = "$5.00 USD"
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"),
        "op1",
        "main",
        plan(fields=[card_binding()], amount_selector="#total" if accepted else None),
        page,
    )
    if accepted:
        assert result["state"] == "completed"
        page.locator.return_value.fill.assert_awaited_once_with("4242424242424242", timeout=15000)
    else:
        assert result["state"] == "reconciling"
        assert store.finish.call_args.args[2] == "reconciling"
        store.consume_resource.assert_not_called()
        page.locator.return_value.fill.assert_not_awaited()
        page.locator.return_value.click.assert_not_awaited()


@pytest.mark.parametrize("frame_origin", [None, "https://evil.example"])
async def test_credential_frame_must_be_the_destination_or_an_allowed_origin(frame_origin):
    store = MagicMock()
    store.consume_resource.return_value = {"password": "private"}
    locator = MagicMock(
        count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), fill=AsyncMock()
    )
    frame = MagicMock(url=frame_origin or "https://shop.example")
    frame.locator.return_value = locator
    element = MagicMock(content_frame=AsyncMock(return_value=frame))
    page = MagicMock(url="https://shop.example/signup")
    page.route = AsyncMock()
    page.locator.return_value.element_handle = AsyncMock(return_value=element)
    binding = FieldBinding(
        selector="#password",
        resource_id="11111111-1111-1111-1111-111111111111",
        kind="credential",
        field="password",
        frame_selector="#frame",
        frame_origin=frame_origin,
    )
    with pytest.raises(PermissionError, match="frame_not_authorized"):
        await BrowserBroker(store)._field(
            Scope(tenant_id="test", owner_id="alice"),
            page,
            binding,
            "https://shop.example",
            frozenset(),
        )
    store.consume_resource.assert_not_called()
    locator.fill.assert_not_awaited()


@pytest.mark.parametrize("count,visible", [(0, True), (2, True), (1, False)])
async def test_ambiguous_or_hidden_selector_is_never_acted_on(count, visible):
    locator = MagicMock(
        count=AsyncMock(return_value=count), is_visible=AsyncMock(return_value=visible)
    )
    with pytest.raises(ValueError, match="selector_not_unique_and_visible"):
        await BrowserBroker(MagicMock())._unique(locator)
    unique = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
    assert await BrowserBroker(MagicMock())._unique(unique) is unique


async def test_duplicate_field_match_never_fills_or_submits(setup):
    store, page = setup
    page.locator.return_value.count = AsyncMock(return_value=2)
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"), "op1", "main", plan(), page
    )
    assert result["state"] == "reconciling"
    page.locator.return_value.fill.assert_not_awaited()
    page.locator.return_value.click.assert_not_awaited()


@pytest.mark.parametrize(
    "kind,code,accepted",
    [
        ("one_time_code", "123456", True),
        ("one_time_code", "12345", False),
        ("one_time_code", "1234567", False),
        ("one_time_code", "12345a", False),
        ("one_time_code", "123456 ", False),
        ("card_code", "739", True),
        ("card_code", "7391", True),
        ("card_code", "73", False),
        ("card_code", "73915", False),
    ],
)
async def test_merchant_verification_code_must_match_the_declared_challenge_format(
    setup, kind, code, accepted
):
    store, page = setup
    payment = kind == "card_code"
    if payment:
        store.operation.return_value["proposal"].update(action="purchase", amount_minor=500)
        page.locator.return_value.inner_text.return_value = "$5.00 USD"
    result = await BrowserBroker(store).execute_on_page(
        Scope(tenant_id="test", owner_id="alice"),
        "op1",
        "main",
        plan(
            fields=[],
            challenge={"selector": "#code", "kind": kind},
            **({"amount_selector": "#total"} if payment else {}),
        ),
        page,
        verification_code=code,
    )
    if accepted:
        assert result["state"] == "completed"
        page.locator.return_value.fill.assert_awaited_once_with(code, timeout=15000)
    else:
        assert result["state"] == "reserved"
        assert result["reason"] == "preflight_failed"
        store.begin_submit.assert_not_called()
        page.locator.return_value.fill.assert_not_awaited()
        page.locator.return_value.click.assert_not_awaited()
