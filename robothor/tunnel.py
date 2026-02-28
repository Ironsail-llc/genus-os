"""
Tunnel / ingress config generation.

Reads enabled Docker Compose profiles and domain config,
then renders a Cloudflare or Caddy config from templates.
"""

from __future__ import annotations

import os
from pathlib import Path

# Service → (subdomain, port) mapping
SERVICE_MAP = {
    "engine": ("engine", 18800),
    "helm": ("app", 3004),
    "bridge": ("bridge", 9100),
    "vision": ("vision", 8600),
    "tts": ("tts", 8880),
    "monitoring": ("status", 3010),
    "camera": ("cam", 8890),
}


def generate_tunnel_config(
    provider: str,
    domain: str,
    enabled_profiles: list[str],
    output_dir: Path | None = None,
) -> Path:
    """Generate tunnel config from templates and enabled profiles.

    Args:
        provider: 'cloudflare' or 'caddy'
        domain: Base domain (e.g. 'robothor.ai')
        enabled_profiles: Docker Compose profiles that are enabled
        output_dir: Where to write the generated config (default: infra/tunnel/)

    Returns:
        Path to the generated config file.
    """
    if output_dir is None:
        output_dir = _find_infra_dir() / "tunnel"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine which optional services are enabled
    tts_enabled = "tts" in enabled_profiles or "full" in enabled_profiles
    monitoring_enabled = "monitoring" in enabled_profiles or "full" in enabled_profiles
    camera_enabled = "media" in enabled_profiles or "full" in enabled_profiles

    tts_port = int(os.environ.get("ROBOTHOR_TTS_PORT", "8880"))
    monitoring_port = int(os.environ.get("ROBOTHOR_MONITORING_PORT", "3010"))
    camera_port = int(os.environ.get("ROBOTHOR_CAMERA_HLS_PORT", "8890"))

    if provider == "cloudflare":
        return _generate_cloudflare(
            domain, output_dir,
            tts_enabled=tts_enabled, tts_port=tts_port,
            monitoring_enabled=monitoring_enabled, monitoring_port=monitoring_port,
            camera_enabled=camera_enabled, camera_port=camera_port,
        )
    elif provider == "caddy":
        return _generate_caddy(
            domain, output_dir,
            tts_enabled=tts_enabled, tts_port=tts_port,
            monitoring_enabled=monitoring_enabled, monitoring_port=monitoring_port,
            camera_enabled=camera_enabled, camera_port=camera_port,
        )
    else:
        raise ValueError(f"Unknown tunnel provider: {provider}. Use 'cloudflare' or 'caddy'.")


def _generate_cloudflare(
    domain: str,
    output_dir: Path,
    *,
    tts_enabled: bool,
    tts_port: int,
    monitoring_enabled: bool,
    monitoring_port: int,
    camera_enabled: bool,
    camera_port: int,
) -> Path:
    template_path = _find_infra_dir() / "tunnel" / "cloudflare.yml.template"
    template = template_path.read_text()

    tunnel_id = os.environ.get("CLOUDFLARE_TUNNEL_ID", "YOUR_TUNNEL_ID")

    tts_ingress = ""
    if tts_enabled:
        tts_ingress = f"  # ── TTS ──\n  - hostname: tts.{domain}\n    service: http://localhost:{tts_port}\n"

    monitoring_ingress = ""
    if monitoring_enabled:
        monitoring_ingress = f"  # ── Monitoring ──\n  - hostname: status.{domain}\n    service: http://localhost:{monitoring_port}\n"

    camera_ingress = ""
    if camera_enabled:
        camera_ingress = f"  # ── Camera ──\n  - hostname: cam.{domain}\n    service: http://localhost:{camera_port}\n"

    content = template.replace("${TUNNEL_ID}", tunnel_id)
    content = content.replace("${DOMAIN}", domain)
    content = content.replace("${TTS_INGRESS}", tts_ingress)
    content = content.replace("${MONITORING_INGRESS}", monitoring_ingress)
    content = content.replace("${CAMERA_INGRESS}", camera_ingress)

    out_path = output_dir / "config.yml"
    out_path.write_text(content)
    return out_path


def _generate_caddy(
    domain: str,
    output_dir: Path,
    *,
    tts_enabled: bool,
    tts_port: int,
    monitoring_enabled: bool,
    monitoring_port: int,
    camera_enabled: bool,
    camera_port: int,
) -> Path:
    template_path = _find_infra_dir() / "tunnel" / "Caddyfile.template"
    template = template_path.read_text()

    tts_block = ""
    if tts_enabled:
        tts_block = f"tts.{domain} {{\n    reverse_proxy localhost:{tts_port}\n}}\n"

    monitoring_block = ""
    if monitoring_enabled:
        monitoring_block = f"status.{domain} {{\n    reverse_proxy localhost:{monitoring_port}\n}}\n"

    camera_block = ""
    if camera_enabled:
        camera_block = f"cam.{domain} {{\n    reverse_proxy localhost:{camera_port}\n}}\n"

    content = template.replace("${DOMAIN}", domain)
    content = content.replace("${TTS_BLOCK}", tts_block)
    content = content.replace("${MONITORING_BLOCK}", monitoring_block)
    content = content.replace("${CAMERA_BLOCK}", camera_block)

    out_path = output_dir / "Caddyfile"
    out_path.write_text(content)
    return out_path


def check_tunnel_status(provider: str) -> dict:
    """Check if the tunnel service is running."""
    import socket
    result: dict = {"provider": provider, "connected": False}
    if provider == "cloudflare":
        try:
            # cloudflared metrics endpoint
            sock = socket.create_connection(("127.0.0.1", 49312), timeout=2)
            sock.close()
            result["connected"] = True
        except (ConnectionRefusedError, OSError, TimeoutError):
            pass
    return result


def _find_infra_dir() -> Path:
    """Locate the infra/ directory."""
    # Development: relative to this file
    repo_root = Path(__file__).parent.parent
    infra = repo_root / "infra"
    if infra.is_dir():
        return infra
    # Fallback: workspace
    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", Path.home() / "robothor"))
    return workspace / "infra"
