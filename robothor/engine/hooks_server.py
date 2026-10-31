"""Standalone webhook receiver: the engine's webhook ingress as its own process.

``python -m robothor.engine.hooks_server`` serves the same handler the engine
mounts (:func:`robothor.engine.webhooks.get_webhook_router`) and nothing else:

- ``POST /api/webhooks/{channel}`` -- verify the signature, claim the delivery
  id in Redis, add the event to the channel's stream, answer 200;
- ``GET /live`` -- 200 while the process serves;
- ``GET /ready`` -- 200 when Redis answers a ping, else 503.

The handler keeps no state of its own (Redis holds the dedupe keys and the
rate-limit counters), so the receiver can run with several replicas and keep
accepting deliveries while the single-writer engine restarts; the engine reads
the streams when it is back. There is no engine authentication, database or
daemon state here: the handler authenticates each raw body with its channel's
own scheme, and the channel listing the engine serves behind its auth is left
out.

Channels come from ``ROBOTHOR_WEBHOOKS_CONFIG`` when it is set (a file, or a
directory holding ``webhooks.yaml`` and ``webhooks.d/``), else from the
workspace's ``docs/webhooks.yaml``. The receiver listens on
``ROBOTHOR_HOOKS_HOST``:``ROBOTHOR_HOOKS_PORT`` (``0.0.0.0:18801``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from robothor.engine import webhooks

if TYPE_CHECKING:
    from robothor.engine.webhooks import WebhookConfig

logger = logging.getLogger(__name__)


def create_app(config: WebhookConfig | None = None) -> FastAPI:
    """The receiver's ASGI app; ``config=None`` loads the configured channels."""
    app = FastAPI(
        title="Genus OS webhook receiver",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(
        webhooks.get_webhook_router(config, include_listing=False, delivery_log=logger)
    )

    @app.get("/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        if not await webhooks.redis_available():
            return JSONResponse({"status": "unavailable", "redis": False}, status_code=503)
        return JSONResponse({"status": "ok", "redis": True})

    return app


def main() -> None:
    """Serve the receiver with uvicorn on the configured host and port."""
    import uvicorn

    from robothor.settings import get_settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = get_settings().engine
    uvicorn.run(
        create_app(),
        host=engine.hooks_host,
        port=int(engine.hooks_port),
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
