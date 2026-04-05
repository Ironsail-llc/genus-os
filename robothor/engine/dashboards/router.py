"""Dashboard routes — replaces brain/ Node.js servers (ports 3000-3003).

Routes are prefixed with /dashboards/ for internal use.
A second router (`public_router`) provides root-level aliases for
Cloudflare tunnel hostnames (e.g., robothor.ai → /, status.robothor.ai → /).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# Public aliases — Cloudflare tunnel sends traffic from robothor.ai,
# status.robothor.ai, ops.robothor.ai, etc. to localhost:18800.
# These root-level routes serve the right dashboard based on Host header.
public_router = APIRouter(tags=["dashboards-public"])

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _get_instance_identity() -> dict[str, str]:
    """Instance contact/brand info for dashboard templates. Read from env vars."""
    import os

    phone = os.environ.get("ROBOTHOR_AI_PHONE", "")
    return {
        "email": os.environ.get("ROBOTHOR_AI_EMAIL", ""),
        "phone": phone,
        "phone_raw": "".join(c for c in phone if c.isdigit() or c == "+"),
        "website": os.environ.get("ROBOTHOR_AI_DOMAIN", ""),
        "website_url": f"https://{os.environ.get('ROBOTHOR_AI_DOMAIN', '')}",
        "telegram_bot": os.environ.get("ROBOTHOR_TELEGRAM_BOT_NAME", ""),
        "ai_name": os.environ.get("ROBOTHOR_AI_NAME", "Genus"),
        "brand_name": os.environ.get("ROBOTHOR_BRAND_NAME", "Genus OS"),
    }


def _render_template(name: str, **context: Any) -> str:
    """Render a Jinja2 template from the templates directory.

    Automatically injects instance identity (email, phone, website, etc.)
    into all template contexts from environment variables.
    """
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template(name)
    return template.render(**_get_instance_identity(), **context)


# ── Status Dashboard (was port 3001) ─────────────────────────────────────────


@router.get("/status", response_class=HTMLResponse)
async def status_dashboard() -> HTMLResponse:
    """Status dashboard — was robothor-status-dashboard on port 3001."""
    from robothor.engine.dashboards.data import get_next_event, get_worker_handoff
    from robothor.engine.dashboards.services import (
        check_all_services,
        get_overall_status,
        get_uptime,
    )
    from robothor.engine.dashboards.theme import brand_css

    services = await check_all_services()
    overall_msg, overall_class = get_overall_status(services)
    handoff = get_worker_handoff()
    next_event = get_next_event()
    uptime = get_uptime()

    html = _render_template(
        "status.html",
        brand_css=brand_css(),
        services=[s.to_dict() for s in services],
        overall_msg=overall_msg,
        overall_class=overall_class,
        escalations=handoff["escalations"],
        next_event=next_event,
        uptime=uptime,
        checked_at=datetime.now(UTC).isoformat(),
    )
    return HTMLResponse(html)


@router.get("/status/api", response_class=JSONResponse)
async def status_api() -> JSONResponse:
    """Status JSON API — was /api/status on port 3001."""
    from robothor.engine.dashboards.data import get_next_event, get_worker_handoff
    from robothor.engine.dashboards.services import (
        check_all_services,
        get_overall_status,
        get_uptime,
    )

    services = await check_all_services()
    overall_msg, overall_class = get_overall_status(services)
    handoff = get_worker_handoff()
    next_event = get_next_event()
    uptime = get_uptime()

    return JSONResponse(
        {
            "services": [s.to_dict() for s in services],
            "overall": overall_msg,
            "overallClass": overall_class,
            "escalations": handoff["escalations"],
            "nextEvent": next_event,
            "uptime": uptime,
            "checkedAt": datetime.now(UTC).isoformat(),
        }
    )


@router.get("/status/webcam")
async def status_webcam() -> Response:
    """Live webcam snapshot — was /api/webcam on port 3001."""
    import asyncio

    snap_path = Path("/tmp/webcam-dashboard-snap.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            "rtsp://localhost:8554/webcam",
            "-frames:v",
            "1",
            "-update",
            "1",
            "-y",
            str(snap_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=8.0)

        if proc.returncode == 0 and snap_path.exists():
            return Response(
                content=snap_path.read_bytes(),
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "no-cache, no-store",
                    "X-Timestamp": datetime.now(UTC).isoformat(),
                },
            )
    except Exception as e:
        logger.warning("Webcam snapshot failed: %s", e)

    return JSONResponse({"error": "webcam unavailable"}, status_code=503)


# ── Ops Dashboard (was port 3003) ────────────────────────────────────────────


@router.get("/ops", response_class=HTMLResponse)
async def ops_dashboard() -> HTMLResponse:
    """Operations dashboard — was brain/dashboard on port 3003."""
    from robothor.engine.dashboards.data import (
        get_calendar,
        get_cron_status,
        get_emails,
        get_jira,
        get_security,
        get_tasks,
    )
    from robothor.engine.dashboards.theme import brand_css

    data = {
        "emails": get_emails(),
        "calendar": get_calendar(),
        "tasks": get_tasks(),
        "jira": get_jira(),
        "security": get_security(),
        "crons": get_cron_status(),
    }

    # Quick service check (simpler than full status dashboard)
    services = []
    for name, port in [("Engine", 18800), ("Voice", 8765), ("RAG", 9099), ("Status", 3000)]:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(f"http://localhost:{port}/health")
                services.append(
                    {
                        "name": name,
                        "port": port,
                        "status": "up" if resp.status_code < 500 else "down",
                    }
                )
        except Exception:
            services.append({"name": name, "port": port, "status": "down"})

    html = _render_template(
        "ops.html",
        brand_css=brand_css(),
        data=data,
        services=services,
        timestamp=datetime.now(UTC).isoformat(),
    )
    return HTMLResponse(html)


@router.get("/ops/api", response_class=JSONResponse)
async def ops_api() -> JSONResponse:
    """Ops data JSON API — was /api/data on port 3003."""
    from robothor.engine.dashboards.data import (
        get_calendar,
        get_cron_status,
        get_emails,
        get_jira,
        get_security,
        get_tasks,
    )

    return JSONResponse(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "emails": get_emails(),
            "calendar": get_calendar(),
            "tasks": get_tasks(),
            "jira": get_jira(),
            "security": get_security(),
            "crons": get_cron_status(),
        }
    )


# ── Homepage (was port 3000) ─────────────────────────────────────────────────


@router.get("/homepage", response_class=HTMLResponse)
async def homepage(request: Request) -> HTMLResponse:
    """Robothor homepage — was robothor-status on port 3000."""
    from robothor.engine.dashboards.data import get_stats
    from robothor.engine.dashboards.services import get_uptime
    from robothor.engine.dashboards.theme import brand_css

    stats = get_stats()
    uptime = get_uptime()

    # Service health strip
    service_defs = [
        {"name": "Engine", "url": "http://localhost:18800/health"},
        {"name": "Voice", "url": "http://localhost:8765/"},
        {"name": "Memory", "url": "http://localhost:9099/health"},
        {"name": "Bridge", "url": "http://localhost:9100/health"},
        {"name": "Helm", "url": "http://localhost:3004/"},
    ]
    service_results = []
    for svc in service_defs:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(svc["url"])
                service_results.append(
                    {**svc, "status": "up" if resp.status_code < 500 else "degraded"}
                )
        except Exception:
            service_results.append({**svc, "status": "down"})

    # Tunnel check
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "is-active", "cloudflared"], capture_output=True, text=True, timeout=3
        )
        tunnel_status = "up" if result.stdout.strip() == "active" else "down"
    except Exception:
        tunnel_status = "down"
    service_results.append({"name": "Tunnel", "status": tunnel_status})

    all_ok = all(r["status"] == "up" for r in service_results)

    # Engine agent fleet
    agents: dict[str, Any] = {}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:18800/health")
            if resp.status_code == 200:
                engine_data = resp.json()
                agents = engine_data.get("agents", {})
    except Exception:
        pass

    # Vision mode
    vision_mode = "unknown"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:8600/health")
            if resp.status_code == 200:
                vision_mode = resp.json().get("mode", "unknown")
    except Exception:
        pass

    html = _render_template(
        "homepage.html",
        brand_css=brand_css(),
        stats=stats,
        uptime=uptime,
        service_results=service_results,
        all_ok=all_ok,
        agents=agents,
        vision_mode=vision_mode,
    )
    return HTMLResponse(html)


# Sub-pages of the homepage
@router.get("/homepage/{page}", response_class=HTMLResponse)
async def homepage_subpage(page: str) -> HTMLResponse:
    """Homepage sub-pages — work-with-me, now, docs, subdomains, contact."""
    from robothor.engine.dashboards.theme import brand_css

    template_map = {
        "work-with-me": "work-with-me.html",
        "now": "now.html",
        "docs": "docs.html",
        "subdomains": "subdomains.html",
        "contact": "contact.html",
    }

    template_name = template_map.get(page)
    if not template_name:
        return HTMLResponse("<h1>404 Not Found</h1>", status_code=404)

    # For the "now" page, pass recent activity
    extra_context: dict[str, Any] = {}
    if page == "now":
        from robothor.engine.dashboards.data import get_stats

        extra_context["stats"] = get_stats()

    html = _render_template(template_name, brand_css=brand_css(), **extra_context)
    return HTMLResponse(html)


# ── Privacy Policy (was port 3002) ───────────────────────────────────────────


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy() -> HTMLResponse:
    """Privacy policy — was brain/privacy-policy on port 3002."""
    from robothor.engine.dashboards.theme import brand_css

    html_path = TEMPLATE_DIR / "privacy.html"
    if html_path.exists():
        html = _render_template("privacy.html", brand_css=brand_css())
        return HTMLResponse(html)
    return HTMLResponse("<h1>Privacy Policy</h1><p>Not found.</p>", status_code=404)


# ── Public routes (Host-header routing for Cloudflare tunnel) ────────────────
#
# Cloudflare tunnel sends traffic from each hostname to localhost:18800.
# These routes detect the Host header and serve the correct dashboard.
# This avoids needing path-based routing in the tunnel config.


def _get_host_dashboard_map() -> dict[str, str]:
    """Build host→dashboard map from ROBOTHOR_AI_DOMAIN env var."""
    import os

    domain = os.environ.get("ROBOTHOR_AI_DOMAIN", "")
    if not domain:
        return {}
    return {
        domain: "homepage",
        f"www.{domain}": "homepage",
        f"status.{domain}": "status",
        f"dashboard.{domain}": "status",
        f"ops.{domain}": "ops",
        f"privacy.{domain}": "privacy",
    }


@public_router.get("/", response_class=HTMLResponse)
async def public_root(request: Request) -> Response:
    """Route based on Host header for Cloudflare tunnel traffic."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    dashboard = _get_host_dashboard_map().get(host)

    if dashboard:
        return RedirectResponse(f"/dashboards/{dashboard}", status_code=307)

    # Default: if accessed directly on engine port, redirect to health
    return RedirectResponse("/health", status_code=307)


@public_router.get("/api/status", response_class=JSONResponse)
async def public_status_api() -> JSONResponse:
    """Public alias for status API (status.robothor.ai/api/status)."""
    return await status_api()


@public_router.get("/api/data", response_class=JSONResponse)
async def public_ops_api() -> JSONResponse:
    """Public alias for ops API (ops.robothor.ai/api/data)."""
    return await ops_api()


@public_router.get("/api/webcam")
async def public_webcam() -> Response:
    """Public alias for webcam (status.robothor.ai/api/webcam)."""
    return await status_webcam()


@public_router.get("/work-with-me", response_class=HTMLResponse)
async def public_work_with_me() -> HTMLResponse:
    """Public alias for robothor.ai/work-with-me."""
    return await homepage_subpage("work-with-me")


@public_router.get("/now", response_class=HTMLResponse)
async def public_now() -> HTMLResponse:
    """Public alias for robothor.ai/now."""
    return await homepage_subpage("now")


@public_router.get("/docs", response_class=HTMLResponse)
async def public_docs() -> HTMLResponse:
    """Public alias for robothor.ai/docs."""
    return await homepage_subpage("docs")


@public_router.get("/subdomains", response_class=HTMLResponse)
async def public_subdomains() -> HTMLResponse:
    """Public alias for robothor.ai/subdomains."""
    return await homepage_subpage("subdomains")


@public_router.get("/contact", response_class=HTMLResponse)
async def public_contact() -> HTMLResponse:
    """Public alias for robothor.ai/contact."""
    return await homepage_subpage("contact")
