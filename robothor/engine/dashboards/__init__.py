"""Dashboard package — consolidates brain/ Node.js servers into FastAPI."""


def get_dashboard_router():
    """Lazy import to avoid requiring FastAPI at module load time."""
    from robothor.engine.dashboards.router import router

    return router


def get_public_router():
    """Public routes for Cloudflare tunnel Host-header routing."""
    from robothor.engine.dashboards.router import public_router

    return public_router
