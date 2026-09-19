"""Rendered public reads retain native URL vetting and bounded GET-only networking."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:password@example.com/",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
    ],
)
async def test_disallowed_targets_never_reach_transport(url, monkeypatch):
    from robothor.engine.tools.handlers import web
    from robothor.engine.web_render_network import RenderError, RenderNetwork

    monkeypatch.setattr(web, "_resolve_and_vet", lambda u: (True, None))
    called = []
    monkeypatch.setattr(web, "_PinnedTransport", lambda b: called.append(b))
    with pytest.raises(RenderError):
        await RenderNetwork().get(url)
    assert called == []


@pytest.mark.asyncio
async def test_redirects_are_rechecked_and_requests_do_not_forward_browser_secrets(monkeypatch):
    from robothor.engine.tools.handlers import web
    from robothor.engine.web_render_network import RenderError, RenderNetwork

    seen, vetted = [], []

    def vet(url):
        vetted.append(url)
        return ("127.0.0.1" in url, "93.184.216.34")

    def transport(backend):
        async def request(req):
            backend.dialed = True
            seen.append(req)
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/secret", "set-cookie": "token=sensitive"},
            )

        return httpx.MockTransport(request)

    monkeypatch.setattr(web, "_resolve_and_vet", vet)
    monkeypatch.setattr(web, "_PinnedTransport", transport)
    with pytest.raises(RenderError):
        await RenderNetwork().get("https://example.com/start")
    assert len(seen) == 1
    assert len(vetted) == 2
    assert seen[0].method == "GET"
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["oversize", "total", "requests", "pin"])
async def test_network_limits_and_actual_pin_are_enforced(fault, monkeypatch):
    from robothor.engine.tools.handlers import web
    from robothor.engine.web_render_network import RenderError, RenderNetwork

    monkeypatch.setattr(web, "_resolve_and_vet", lambda u: (False, "93.184.216.34"))

    def transport(backend):
        async def request(req):
            backend.dialed = fault != "pin"
            return httpx.Response(200, content=b"payload")

        return httpx.MockTransport(request)

    monkeypatch.setattr(web, "_PinnedTransport", transport)
    net = RenderNetwork()
    if fault == "oversize":
        net.max_resource_bytes = 3
    if fault == "total":
        net.max_total_bytes = 3
    if fault == "requests":
        net.max_requests = 0
    with pytest.raises(RenderError):
        await net.get("https://example.com/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,resource", [("POST", "fetch"), ("PUT", "xhr"), ("GET", "image"), ("GET", "websocket")]
)
async def test_browser_route_cannot_write_or_load_nonessential_resources(method, resource):
    from robothor.engine.tools.handlers.web_render import RenderRoutes

    net = SimpleNamespace(get=AsyncMock())
    route = SimpleNamespace(
        request=SimpleNamespace(method=method, resource_type=resource, url="https://example.com/"),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
    )
    await RenderRoutes(net).handle(route)
    route.abort.assert_awaited_once()
    net.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_tool_refuses_arbitrary_browser_operations():
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.web_render import HANDLERS

    result = await HANDLERS["web_render"](
        {"url": "https://example.com/", "script": "fetch(secret)"}, ToolContext()
    )
    assert "error" in result


def test_render_is_a_narrow_registered_read_tool():
    from robothor.engine.tools.constants import READONLY_TOOLS
    from robothor.engine.tools.dispatch import builtin_handlers
    from robothor.engine.tools.schemas import get_engine_schemas
    from robothor.sales.research_manifest import READ_TOOLS

    assert "web_render" in builtin_handlers()
    schema = get_engine_schemas()["web_render"]["function"]["parameters"]
    assert schema["required"] == ["url"]
    assert set(schema["properties"]) == {"url"}
    assert schema["additionalProperties"] is False
    assert "web_render" in READONLY_TOOLS & READ_TOOLS


@pytest.mark.asyncio
async def test_real_browser_renders_javascript_and_drops_page_credentials(monkeypatch):
    """Opt-in installed-browser integration; every upstream response is a fixture."""
    import os

    if os.environ.get("ROBOTHOR_TEST_RENDER_BROWSER") != "1":
        pytest.skip("Set ROBOTHOR_TEST_RENDER_BROWSER=1 for installed Chromium verification")
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import web
    from robothor.engine.tools.handlers.web_render import web_render

    seen = []
    monkeypatch.setattr(web, "_resolve_and_vet", lambda u: (False, "93.184.216.34"))

    def transport(backend):
        async def request(req):
            backend.dialed = True
            seen.append(req)
            assert req.method == "GET"
            assert "authorization" not in req.headers and "cookie" not in req.headers
            if req.url.path == "/":
                body = '<html><body><script src="/app.js"></script></body></html>'
                mime = "text/html"
            elif req.url.path == "/app.js":
                body = """document.cookie = 'private=value';
                fetch('/blocked-write', {method:'POST', body:'should never send'}).catch(()=>{});
                fetch('/data', {headers:{Authorization:'Bearer page-secret'}})
                  .then(r=>r.text()).then(t=>{document.body.innerHTML='<main>'+t+'</main><a href="/team">Team</a>';});"""
                mime = "text/javascript"
            elif req.url.path == "/data":
                body = "Verified JavaScript-rendered business page"
                mime = "text/plain"
            else:
                raise AssertionError("Unexpected target")
            return httpx.Response(200, content=body, headers={"content-type": mime})

        return httpx.MockTransport(request)

    monkeypatch.setattr(web, "_PinnedTransport", transport)
    result = await web_render({"url": "https://clinic.example.com/"}, ToolContext())
    assert "error" not in result, result
    assert "Verified JavaScript-rendered business page" in result["content"]
    assert result["links"] == [{"url": "https://clinic.example.com/team", "text": "Team"}]
    assert result["blocked_resources"] >= 1
    assert {r.url.path for r in seen} == {"/", "/app.js", "/data"}


@pytest.mark.asyncio
async def test_document_routes_do_not_admit_popups_or_subframes():
    from robothor.engine.tools.handlers.web_render import RenderRoutes

    initial = {"url": "https://example.com/", "status": 200, "headers": {}, "body": b"<html/>"}
    routes = RenderRoutes(SimpleNamespace(get=AsyncMock()), initial)
    routes.page = "main-page"
    route = SimpleNamespace(
        request=SimpleNamespace(
            method="GET",
            resource_type="document",
            url=initial["url"],
            frame=SimpleNamespace(page="popup", parent_frame=None),
        ),
        abort=AsyncMock(),
        fulfill=AsyncMock(),
    )
    await routes.handle(route)
    route.abort.assert_awaited_once()
    route.fulfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_render_does_not_keep_resolving_new_hosts(monkeypatch):
    from unittest.mock import Mock

    from robothor.engine.tools.handlers import web
    from robothor.engine.web_render_network import RenderError, RenderNetwork

    vet = Mock(return_value=(False, "93.184.216.34"))
    monkeypatch.setattr(web, "_resolve_and_vet", vet)
    net = RenderNetwork()
    net.max_requests = 0
    with pytest.raises(RenderError):
        await net.get("https://example.com/")
    vet.assert_not_called()


@pytest.mark.asyncio
async def test_document_fragment_is_not_part_of_the_network_identity(monkeypatch):
    from robothor.engine.tools.handlers import web
    from robothor.engine.web_render_network import RenderNetwork

    monkeypatch.setattr(web, "_resolve_and_vet", lambda u: (False, "93.184.216.34"))

    def transport(backend):
        async def request(req):
            backend.dialed = True
            return httpx.Response(200, content="page")

        return httpx.MockTransport(request)

    monkeypatch.setattr(web, "_PinnedTransport", transport)
    response = await RenderNetwork().get("https://example.com/#services")
    assert response["url"] == "https://example.com/"
