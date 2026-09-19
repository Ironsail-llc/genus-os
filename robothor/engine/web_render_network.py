"""Bounded public GET transport for rendering; the browser never opens target sockets."""

import asyncio
from urllib.parse import urlsplit

import httpx


class RenderError(ValueError):
    pass


def public_url(url):
    try:
        p = urlsplit(url)
        return (
            isinstance(url, str)
            and p.scheme in {"http", "https"}
            and bool(p.hostname)
            and not p.username
            and not p.password
        )
    except (ValueError, TypeError):
        return False


class RenderNetwork:
    max_requests = 40
    max_resource_bytes = 4 * 1024 * 1024
    max_total_bytes = 12 * 1024 * 1024

    def __init__(self):
        self.requests = 0
        self.bytes = 0
        self.slots = asyncio.Semaphore(4)

    async def get(self, url):
        async with self.slots:
            for _ in range(7):
                response = await self._one(url)
                if response["status"] in (301, 302, 303, 307, 308):
                    location = response["headers"].get("location")
                    if not location:
                        raise RenderError("Redirect has no location")
                    url = str(httpx.URL(url).join(location))
                else:
                    return response
            raise RenderError("Render redirect limit exceeded")

    async def _one(self, url):
        from robothor.engine.tools.handlers import web

        if not public_url(url):
            raise RenderError("Render requires a public HTTP(S) URL without credentials")
        if self.requests >= self.max_requests:
            raise RenderError("Render request limit exceeded")
        self.requests += 1
        blocked, ip = await asyncio.to_thread(web._resolve_and_vet, url)
        if blocked or not ip:
            raise RenderError("Render target is not a vetted public address")
        backend = web._PinnedResolutionBackend()
        parsed = httpx.URL(url).copy_with(fragment=None)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        backend.pin(parsed.host, port, ip)
        backend.pin(parsed.raw_host.decode("ascii"), port, ip)
        async with httpx.AsyncClient(
            transport=web._PinnedTransport(backend),
            timeout=10,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": web._user_agent(), "Accept": "*/*"},
        ) as client:
            async with client.stream("GET", url) as response:
                if not backend.dialed:
                    raise RenderError("Render transport bypassed the vetted address pin")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    self.bytes += len(chunk)
                    if (
                        len(body) + len(chunk) > self.max_resource_bytes
                        or self.bytes > self.max_total_bytes
                    ):
                        raise RenderError("Render byte limit exceeded")
                    body.extend(chunk)
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key
                    in {
                        "content-type",
                        "location",
                        "content-security-policy",
                        "access-control-allow-origin",
                        "access-control-allow-methods",
                        "access-control-allow-headers",
                        "cross-origin-resource-policy",
                    }
                }
                return {
                    "url": str(parsed),
                    "status": response.status_code,
                    "headers": headers,
                    "body": bytes(body),
                }
