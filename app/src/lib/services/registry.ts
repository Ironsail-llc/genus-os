/**
 * Service Registry — TypeScript client for robothor-services.json.
 *
 * Server-side only (reads manifest from filesystem).
 * Environment variables override manifest defaults.
 */

import fs from "fs";
import path from "path";

interface ServiceDef {
  name: string;
  port: number;
  host: string;
  health: string | null;
  protocol?: string;
  systemd_unit?: string | null;
  tunnel_route?: string | null;
  dependencies: string[];
  extra_ports?: Record<string, number>;
}

interface Manifest {
  version: string;
  services: Record<string, ServiceDef>;
}

/** Environment variable overrides per service */
const ENV_OVERRIDES: Record<string, string> = {
  bridge: "BRIDGE_URL",
  orchestrator: "ORCHESTRATOR_URL",
  vision: "VISION_URL",
  ollama: "OLLAMA_URL",
  redis: "REDIS_URL",
  searxng: "SEARXNG_URL",
  vaultwarden: "VAULTWARDEN_URL",
  impetus_one: "IMPETUS_ONE_BASE_URL",
  helm: "HELM_URL",
};

const MANIFEST_PATHS = [
  path.resolve(__dirname, "../../../../robothor-services.json"),
  path.join(process.env.ROBOTHOR_WORKSPACE || path.join(process.env.HOME || "", "robothor"), "robothor-services.json"),
];

let _manifest: Manifest | null = null;

function loadManifest(): Manifest {
  if (_manifest) return _manifest;

  for (const p of MANIFEST_PATHS) {
    try {
      const content = fs.readFileSync(p, "utf-8");
      _manifest = JSON.parse(content) as Manifest;
      return _manifest;
    } catch {
      continue;
    }
  }

  console.warn("Service manifest not found");
  _manifest = { version: "0.0.0", services: {} };
  return _manifest;
}

/**
 * Get the base URL for a service, optionally with a path appended.
 * Environment variable overrides take precedence.
 */
export function getServiceUrl(name: string, urlPath = ""): string | null {
  // Check env override
  const envKey = ENV_OVERRIDES[name];
  if (envKey) {
    const envVal = process.env[envKey];
    if (envVal) {
      const base = envVal.replace(/\/$/, "");
      return urlPath ? `${base}${urlPath}` : base;
    }
  }

  const manifest = loadManifest();
  const svc = manifest.services[name];
  if (!svc) return null;

  const protocol = svc.protocol === "ws" ? "ws" : "http";
  const base = `${protocol}://${svc.host}:${svc.port}`;
  return urlPath ? `${base}${urlPath}` : base;
}

/**
 * Get the health check URL for a service.
 */
export function getHealthUrl(name: string): string | null {
  const manifest = loadManifest();
  const svc = manifest.services[name];
  if (!svc?.health) return null;
  return getServiceUrl(name, svc.health);
}

/**
 * List all services.
 */
export function listServices(): Record<string, ServiceDef> {
  return loadManifest().services;
}

/** Reset cache for testing */
export function _resetCache(): void {
  _manifest = null;
}
