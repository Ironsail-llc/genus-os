"""Dashboard package — consolidates brain/ Node.js servers into FastAPI."""

from typing import Any


def get_dashboard_router() -> Any:
    """Lazy import to avoid requiring FastAPI at module load time."""
    from robothor.engine.dashboards.router import router

    return router


def get_public_router() -> Any:
    """Public routes for Cloudflare tunnel Host-header routing."""
    from robothor.engine.dashboards.router import public_router

    return public_router
