"""Selected material documents are isolated reads, not authority from page links."""

import json

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.terms_audit import TermsAudit
from robothor.autonomy.tests.test_terms_audit import operation


@pytest.mark.parametrize("external_document", [False, True])
@pytest.mark.timeout(60)
async def test_selected_html_terms_are_captured_without_parent_cookies_or_scripts(
    store, identity, external_document
):
    op = operation(store, identity, allow_any_website=external_document)
    target = "https://legal.example/terms" if external_document else "https://club.example/terms"
    requests = []

    async def website(route):
        requests.append((route.request.url, route.request.headers.get("cookie", "")))
        if route.request.url.endswith("/terms"):
            await route.fulfill(
                body='<body>Annual membership renews on the agreed date.<script>fetch("/script-ran")</script></body>',
                content_type="text/html",
            )
        else:
            await route.fulfill(
                body=f'<body><a id="terms" href="{target}">Membership terms</a><button id="submit" onclick="document.querySelector(\'#done\').hidden=false">Apply</button><p id="done" hidden>Application received</p></body>',
                content_type="text/html",
            )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(
            [
                {
                    "name": "account_session",
                    "value": "private-session-value",
                    "domain": "club.example",
                    "path": "/",
                    "secure": True,
                }
            ]
        )
        await context.route("**/*", website)
        page = await context.new_page()
        broker = BrowserBroker(store, public_document_router=website)
        result = await broker.execute_on_page(
            identity,
            op["id"],
            "main",
            ExecutionPlan(
                url="https://club.example/apply",
                submit_selector="#submit",
                success_selector="#done",
                success_text="Application received",
                material_terms=[{"selector": "#terms"}],
            ),
            page,
        )
        await browser.close()
    assert result["state"] == "completed"
    assert "Annual membership" not in json.dumps(result)
    reads = [headers for url, headers in requests if url.endswith("/terms")]
    assert len(reads) == 2 and reads == ["", ""]
    assert not any(url.endswith("/script-ran") for url, _ in requests)
    audit = TermsAudit(store)
    snapshots = [
        audit.read(identity, op["id"], r["id"])["snapshot"] for r in audit.list(identity, op["id"])
    ]
    assert len(snapshots) == 2
    for snapshot in snapshots:
        assert snapshot["coverage"] == "visible_text_and_selected_documents"
        documents = [doc for doc in snapshot["documents"] if doc["source"] == "linked_document"]
        assert len(documents) == 1 and "Annual membership" in documents[0]["text"]
        assert documents[0]["source_url"] == target


@pytest.mark.parametrize("redirect", [False, True])
@pytest.mark.timeout(60)
async def test_unauthorized_document_destination_never_receives_request_or_submits(
    store, identity, redirect
):
    op = operation(store, identity)
    requests = []

    async def website(route):
        requests.append(route.request.url)
        if route.request.url.endswith("/redirect"):
            await route.fulfill(status=302, headers={"location": "https://foreign.example/terms"})
            return
        href = "/redirect" if redirect else "https://foreign.example/terms"
        await route.fulfill(
            body=f'<body><a id="terms" href="{href}">Terms</a><button id="submit">Apply</button><p id="done" hidden>Application received</p></body>',
            content_type="text/html",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", website)
        result = await BrowserBroker(store, public_document_router=website).execute_on_page(
            identity,
            op["id"],
            "main",
            ExecutionPlan(
                url="https://club.example/apply",
                submit_selector="#submit",
                success_selector="#done",
                success_text="Application received",
                material_terms=[{"selector": "#terms"}],
            ),
            page,
        )
        await browser.close()
    assert result["state"] == "reserved" and result["reason"] == "material_terms_unavailable"
    assert requests == ["https://club.example/apply"] + (
        ["https://club.example/redirect"] if redirect else []
    )
    assert store.operation(identity, op["id"])["state"] == "reserved"


@pytest.mark.timeout(60)
async def test_inspection_offers_terms_selectors_without_private_link_urls(store):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            "https://club.example/**",
            lambda route: route.fulfill(
                body='<body><a id="legal" href="/terms?private-token=secret">Membership terms</a></body>',
                content_type="text/html",
            ),
        )
        await page.goto("https://club.example/apply")
        result = await BrowserBroker(store).inspect(page, "https://club.example", frozenset())
        await browser.close()
    assert result["terms_links"] == [{"selector": "#legal", "label": "Membership terms"}]
    assert "private-token" not in json.dumps(result)


def test_empty_selection_preserves_preexisting_plan_and_rpc_fingerprints():
    from uuid import uuid4

    from robothor.autonomy.workflows.protocol import ExecuteRequest
    from robothor.autonomy.workflows.store import fingerprint

    plan = ExecutionPlan(url="https://club.example/apply", submit_selector="#submit")
    command = ExecuteRequest(
        kind="execute", workflow_id=uuid4(), command_id=uuid4(), revision=0, plan=plan
    )
    serialized = command.model_dump(mode="json")
    legacy = json.loads(json.dumps(serialized))
    legacy["plan"].pop("material_terms", None)
    assert serialized == legacy
    assert fingerprint(serialized) == fingerprint(legacy)


@pytest.mark.parametrize(
    "body,status,media",
    [
        ("Missing", 404, "text/html"),
        ('<input type="password">Sign in', 200, "text/html"),
        ("", 200, "text/html"),
        ("x" * 200001, 200, "text/html"),
        ("PDF bytes", 200, "application/pdf"),
    ],
    ids=["missing", "login", "empty", "oversized", "pdf"],
)
@pytest.mark.timeout(60)
async def test_unavailable_required_documents_stop_before_private_fill(
    store, identity, body, status, media
):
    from robothor.autonomy.broker import FieldBinding
    from robothor.autonomy.models import ResourceInput

    op = operation(store, identity)
    credential = store.put_resource(
        identity,
        ResourceInput(
            kind="credential",
            label="Fixture login",
            origin="https://club.example",
            payload=json.dumps({"username": "fixture", "password": "private-fixture-password"}),
        ),
    )

    async def website(route):
        if route.request.url.endswith("/terms"):
            await route.fulfill(status=status, body=body, content_type=media)
        else:
            await route.fulfill(
                body='<body><a id="terms" href="/terms">Terms</a><input id="password" type="password"><button id="submit">Apply</button><p id="done" hidden>Application received</p></body>',
                content_type="text/html",
            )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", website)
        result = await BrowserBroker(store, public_document_router=website).execute_on_page(
            identity,
            op["id"],
            "main",
            ExecutionPlan(
                url="https://club.example/apply",
                submit_selector="#submit",
                success_selector="#done",
                success_text="Application received",
                material_terms=[{"selector": "#terms"}],
                fields=[
                    FieldBinding(
                        selector="#password",
                        resource_id=credential["id"],
                        kind="credential",
                        field="password",
                    )
                ],
            ),
            page,
        )
        assert await page.locator("#password").input_value() == ""
        await browser.close()
    assert result["state"] == "reserved" and result["reason"] == "material_terms_unavailable"
    assert store.operation(identity, op["id"])["execution_plan"] is None


@pytest.mark.timeout(60)
async def test_failed_selection_can_be_corrected_on_the_same_operation(store, identity):
    op = operation(store, identity)

    async def website(route):
        if route.request.url.endswith("/terms"):
            await route.fulfill(body="Annual membership terms", content_type="text/plain")
        else:
            await route.fulfill(
                body='<body><a id="terms" href="/terms">Terms</a><button id="submit" onclick="document.querySelector(\'#done\').hidden=false">Apply</button><p id="done" hidden>Application received</p></body>',
                content_type="text/html",
            )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", website)
        broker = BrowserBroker(store, public_document_router=website)

        def plan(selector):
            return ExecutionPlan(
                url="https://club.example/apply",
                submit_selector="#submit",
                success_selector="#done",
                success_text="Application received",
                material_terms=[{"selector": selector}],
            )

        first = await broker.execute_on_page(identity, op["id"], "main", plan("#missing"), page)
        assert first["reason"] == "material_terms_unavailable"
        final = await broker.execute_on_page(identity, op["id"], "main", plan("#terms"), page)
        await browser.close()
    assert final["state"] == "completed"
