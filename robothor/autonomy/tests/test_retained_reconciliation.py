"""An unfamiliar confirmation must remain recoverable without another submit."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import ExecutionPlan
from robothor.autonomy.models import Delegation, RuntimeSettings, WebOperation
from robothor.autonomy.workflows.manager import WorkflowManager


@pytest.mark.timeout(45)
async def test_uncertain_account_retains_confirmation_for_read_only_reconciliation(
    store, identity, monkeypatch
):
    from robothor.autonomy.workflows import outcome

    monkeypatch.setattr(outcome, "OUTCOME_TIMEOUT_SECONDS", 0.1)
    grant = store.create_grant(
        identity,
        Delegation(
            agent_ids={"main"},
            origins={"https://account.example"},
            actions={"account"},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    op = store.reserve(
        identity,
        grant["id"],
        "main",
        WebOperation(
            origin="https://account.example",
            action="account",
            purpose="Requested account",
            idempotency_key="retained-account-confirmation",
        ),
    )
    requests = []

    async def merchant(route):
        requests.append((route.request.method, route.request.url))
        if route.request.method == "POST":
            await route.fulfill(body="ok")
        else:
            await route.fulfill(
                content_type="text/html",
                body="""
                <button id="submit" onclick="fetch('/register',{method:'POST'}).then(()=>{
                document.querySelector('#confirmation').textContent='Account created. Email verification required.';
                })">Create account</button><p id="confirmation"></p>""",
            )

    async with async_playwright() as pw:

        async def factory():
            return await pw.chromium.launch(headless=True)

        manager = WorkflowManager(store, factory, request_router=merchant)
        try:
            opened = await manager.open(
                identity, "main", op["id"], "https://account.example/register"
            )
            wid = opened["workflow_id"]
            plan = ExecutionPlan(url="https://account.example/register", submit_selector="#submit")
            result = await manager.execute(identity, "main", wid, str(uuid4()), 0, plan)
            assert result["state"] == "reconciling"
            assert sum(method == "POST" for method, _ in requests) == 1
            assert manager.active_count == 1, "The only confirmation page was discarded"
            observed = await manager.inspect(identity, "main", wid)
            confirmation = next(
                item for item in observed["confirmations"] if "Account created" in item["text"]
            )
            before = list(requests)
            mismatch = await manager.reconcile(
                identity,
                "main",
                wid,
                str(uuid4()),
                0,
                confirmation["selector"],
                "Unobserved success",
            )
            assert mismatch["state"] == "reconciling"
            assert manager.active_count == 1
            with pytest.raises(PermissionError):
                await manager.reconcile(
                    identity,
                    "main",
                    wid,
                    str(uuid4()),
                    5,
                    confirmation["selector"],
                    confirmation["text"],
                )
            retry = await manager.execute(identity, "main", wid, str(uuid4()), 0, plan)
            assert retry["state"] == "reconciling" and manager.active_count == 1
            with pytest.raises(PermissionError):
                await manager.inspect(
                    identity.model_copy(update={"owner_id": "other"}), "main", wid
                )
            with pytest.raises(PermissionError):
                await manager.reconcile(
                    identity,
                    "other-agent",
                    wid,
                    str(uuid4()),
                    0,
                    confirmation["selector"],
                    confirmation["text"],
                )
            await manager._live[wid].page.evaluate(
                "fetch('/extra-submit',{method:'POST'}).catch(()=>null)"
            )
            assert requests == before
            # Revocation stops new actions, but read-only reconciliation must survive.
            store.revoke_grant(identity, grant["id"])
            store.configure(identity, RuntimeSettings(enabled=False))
            command = str(uuid4())
            import httpx

            from robothor.auth import tokens
            from robothor.autonomy.workflows.api import create_app
            from robothor.autonomy.workflows.client import invoke

            monkeypatch.setattr(tokens, "signing_key", lambda: "fixture-only-key-" * 3)
            recovered = await invoke(
                identity,
                "main",
                {
                    "kind": "reconcile",
                    "workflow_id": wid,
                    "command_id": command,
                    "revision": 0,
                    "selector": confirmation["selector"],
                    "text": confirmation["text"],
                },
                transport=httpx.ASGITransport(app=create_app(manager)),
            )
            assert recovered["state"] == "completed"
            assert store.operation(identity, op["id"])["state"] == "completed"
            assert requests == before
            assert sum(method == "POST" for method, _ in requests) == 1
            assert (
                await manager.reconcile(
                    identity,
                    "main",
                    wid,
                    command,
                    0,
                    confirmation["selector"],
                    confirmation["text"],
                )
                == recovered
            )
            assert manager.active_count == 0
        finally:
            await manager.shutdown()


async def test_candidate_evidence_excludes_prior_hidden_editable_and_masks_reflections():
    import base64
    from types import SimpleNamespace

    from robothor.autonomy.privacy import ProtectedValues
    from robothor.autonomy.workflows.retained import candidates, messages, public_message

    secret = "PrivateCanaryQ9"
    protected = ProtectedValues()
    protected.add(secret)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route(
                "**/*",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body="""
                <p>Previous receipt</p><p hidden>Hidden success</p><p contenteditable>Editable success</p>
                <textarea>Form success</textarea><p id="new"></p>""",
                ),
            )
            await page.goto("https://account.example/register")
            baseline = {item["digest"] for item in await messages(page, "https://account.example")}
            await page.locator("#new").evaluate(
                "(e,s)=>{e.id=s;e.textContent='Receipt issued for '+s;}",
                base64.b64encode(secret.encode()).decode(),
            )
            live = SimpleNamespace(
                page=page,
                broker=SimpleNamespace(
                    _used_transient_code=False,
                    reconciliation_baseline=baseline,
                    protected_values=protected,
                ),
            )
            results = [
                public_message(item, protected)
                for item in await candidates(live, "https://account.example")
            ]
            assert len(results) == 1
            assert results[0]["text"] == "Receipt issued for [private]"
            assert "#" not in results[0]["selector"]
            await page.evaluate(
                "document.body.insertAdjacentHTML('beforeend','<p>Bounded observation</p>'.repeat(81))"
            )
            assert await messages(page, "https://account.example") is None
            assert await candidates(live, "https://account.example") == []
            live.broker._used_transient_code = True
            assert await candidates(live, "https://account.example") == []
        finally:
            await browser.close()


@pytest.mark.parametrize(
    "extra", [{"url": "https://other.example"}, {"fields": []}, {"verification_code": "123456"}]
)
def test_reconciliation_rpc_cannot_navigate_fill_or_supply_codes(extra):
    from pydantic import ValidationError

    from robothor.autonomy.workflows.protocol import RPC

    with pytest.raises(ValidationError):
        RPC.validate_python(
            {
                "kind": "reconcile",
                "workflow_id": str(uuid4()),
                "command_id": str(uuid4()),
                "revision": 0,
                "selector": "p",
                "text": "Account created",
                **extra,
            }
        )
