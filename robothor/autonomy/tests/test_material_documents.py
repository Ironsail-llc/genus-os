"""Selected material documents are isolated reads, not authority from page links."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from robothor.autonomy.broker import BrowserBroker, ExecutionPlan
from robothor.autonomy.terms_audit import TermsAudit
from robothor.autonomy.tests.test_terms_audit import operation


@pytest.mark.parametrize(
    "target,origins",
    [
        ("https://club.example/terms", ("https://club.example",)),
        ("https://legal.example/terms", ("https://club.example", "https://legal.example")),
    ],
    ids=["operation_origin", "authorized_other_origin"],
)
@pytest.mark.timeout(60)
async def test_selected_html_terms_are_captured_without_parent_cookies_or_scripts(
    store, identity, target, origins
):
    op = operation(store, identity, origins=origins)
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


@pytest.mark.timeout(60)
async def test_allow_any_website_does_not_widen_material_document_destinations(store, identity):
    """Browsing authority is not document authority: a link off the grant is refused."""
    op = operation(store, identity, allow_any_website=True)
    requests = []

    async def website(route):
        requests.append(route.request.url)
        if route.request.url.endswith("/terms"):
            await route.fulfill(
                body="<body>Annual membership renews on the agreed date.</body>",
                content_type="text/html",
            )
        else:
            await route.fulfill(
                body='<body><a id="terms" href="https://legal.example/terms">Terms</a><button id="submit" onclick="document.querySelector(\'#done\').hidden=false">Apply</button><p id="done" hidden>Application received</p></body>',
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
    assert requests == ["https://club.example/apply"]
    assert store.operation(identity, op["id"])["state"] == "reserved"


@pytest.mark.timeout(60)
async def test_allow_any_website_does_not_widen_a_document_redirect(store, identity):
    """The re-check after a redirect is bound by the same authority as the request."""
    op = operation(store, identity, allow_any_website=True)
    requests = []

    async def website(route):
        requests.append(route.request.url)
        if route.request.url.endswith("/redirect"):
            await route.fulfill(status=302, headers={"location": "https://foreign.example/terms"})
        elif route.request.url.endswith("/terms"):
            await route.fulfill(body="<body>Terms</body>", content_type="text/html")
        else:
            await route.fulfill(
                body='<body><a id="terms" href="/redirect">Terms</a><button id="submit">Apply</button><p id="done" hidden>Application received</p></body>',
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
    # Host equality, not a prefix: `startswith("https://foreign.example")`
    # also matches `https://foreign.example.attacker.test`, which is the
    # substring-sanitization shape CodeQL flags. The claim here is that no
    # request reached that origin, so compare the parsed host.
    hosts = {urlsplit(url).hostname for url in requests}
    assert "foreign.example" not in hosts


@pytest.mark.timeout(60)
async def test_a_document_on_a_private_address_is_refused_by_the_real_router(store, identity):
    """No stub router: the request crosses robothor.autonomy.worker.public_request."""
    op = operation(store, identity, origins=("https://club.example", "https://127.0.0.1:9"))

    async def website(route):
        await route.fulfill(
            body='<body><a id="terms" href="https://127.0.0.1:9/terms">Terms</a><button id="submit">Apply</button><p id="done" hidden>Application received</p></body>',
            content_type="text/html",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route("**/*", website)
        result = await BrowserBroker(store).execute_on_page(
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
    assert store.operation(identity, op["id"])["state"] == "reserved"


class StubRoute:
    """A route that records the decision instead of reaching a network."""

    def __init__(self, url, method="GET", headers=None):
        self.request = SimpleNamespace(
            url=url, method=method, headers=headers or {}, resource_type="document"
        )
        self.calls = []
        self.fulfilled = {}

    async def abort(self, *args, **kwargs):
        self.calls.append("abort")

    async def continue_(self, *args, **kwargs):
        self.calls.append("continue")

    async def fulfill(self, **kwargs):
        self.calls.append("fulfill")
        self.fulfilled = kwargs


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/terms",
        "https://localhost/terms",
        "https://[::1]/terms",
        "https://10.1.2.3/terms",
        "https://192.168.1.10/terms",
        "https://169.254.169.254/latest/meta-data",
        "ftp://93.184.216.34/terms",
        "https:///terms",
    ],
)
async def test_public_request_refuses_every_destination_that_is_not_public(url):
    from robothor.autonomy.worker import public_request

    route = StubRoute(url)
    await public_request(route)
    assert route.calls == ["abort"]


async def test_public_request_lets_a_public_destination_through():
    from robothor.autonomy.worker import public_request

    # A literal address: getaddrinfo answers it without a resolver, and the
    # stub route means nothing is ever connected to.
    route = StubRoute("https://93.184.216.34/terms")
    await public_request(route)
    assert route.calls == ["continue"]


async def test_public_document_request_refuses_a_private_destination():
    from robothor.autonomy.worker import public_document_request

    route = StubRoute("https://127.0.0.1/terms")
    await public_document_request(route)
    assert route.calls == ["abort"]


async def local_document_server(response):
    """Serve one fixed HTTP response on the loopback interface."""

    async def handle(reader, writer):
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(response)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def any_destination(url):
    return True


@pytest.mark.parametrize("case", ["ordinary", "declared_oversize", "undeclared_oversize"])
@pytest.mark.timeout(60)
async def test_public_document_request_bounds_the_bytes_it_accepts(monkeypatch, case):
    from robothor.autonomy import worker

    if case == "ordinary":
        body = b"<body>Annual membership renews on the agreed date.</body>"
        header = f"Content-Length: {len(body)}\r\n"
    elif case == "declared_oversize":
        # A declared 40 MB document is refused before a byte of it is read.
        body = b"x" * 1000
        header = "Content-Length: 41943040\r\n"
    else:
        body = b"x" * (worker.MAX_DOCUMENT_BYTES + 1024)
        header = "Connection: close\r\n"
    response = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n{header}\r\n".encode() + body
    server, port = await local_document_server(response)
    # The address check has tests of its own; this one bounds the transfer.
    monkeypatch.setattr(worker, "public_destination", any_destination)
    route = StubRoute(f"http://127.0.0.1:{port}/terms")
    try:
        await worker.public_document_request(route)
    finally:
        server.close()
        await server.wait_closed()
    assert route.calls == (["fulfill"] if case == "ordinary" else ["abort"])
    if case == "ordinary":
        assert route.fulfilled["body"] == body
        assert route.fulfilled["headers"]["content-type"] == "text/html"
