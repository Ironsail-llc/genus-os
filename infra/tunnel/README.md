# Tunnel / Ingress Configuration

Robothor needs external access for Telegram webhooks, voice calls, and mobile access.
Two providers are supported out of the box:

## Cloudflare Tunnel (Recommended)

Zero-trust tunnel — no open ports, Cloudflare handles DNS and TLS.

1. Create a tunnel: `cloudflared tunnel create robothor`
2. Set env vars:
   ```
   ROBOTHOR_TUNNEL_PROVIDER=cloudflare
   ROBOTHOR_DOMAIN=yourdomain.com
   CLOUDFLARE_TUNNEL_TOKEN=<token>
   ```
3. Generate config: `robothor tunnel generate`
4. Start: `docker compose --profile tunnel up -d`

## Caddy (Self-hosted alternative)

Automatic HTTPS via Let's Encrypt. Requires ports 80/443 open.

1. Install Caddy: https://caddyserver.com/docs/install
2. Set env vars:
   ```
   ROBOTHOR_TUNNEL_PROVIDER=caddy
   ROBOTHOR_DOMAIN=yourdomain.com
   ```
3. Generate config: `robothor tunnel generate`
4. Copy generated Caddyfile to `/etc/caddy/Caddyfile`
5. Reload: `sudo systemctl reload caddy`

## Generated Files

- `infra/tunnel/config.yml` — Cloudflare tunnel ingress rules
- `infra/tunnel/Caddyfile` — Caddy reverse proxy config

These files are generated from templates and should not be edited manually.
Regenerate after enabling/disabling services: `robothor tunnel generate`
