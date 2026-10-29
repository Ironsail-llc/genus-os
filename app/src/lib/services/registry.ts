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
  protocol?: "http" | "https";
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
  engine: "ROBOTHOR_ENGINE_URL",
  bridge: "BRIDGE_URL",
  orchestrator: "ORCHESTRATOR_URL",
  vision: "VISION_URL",
  ollama: "OLLAMA_URL",
  redis: "REDIS_URL",
  searxng: "SEARXNG_URL",
  helm: "HELM_URL",
};

function validatedServiceUrl(baseValue: string, urlPath = ""): string | null {
  if (baseValue.length > 2_048) return null;

  try {
    const base = new URL(baseValue);
    if (
      !["http:", "https:"].includes(base.protocol) ||
      !base.hostname ||
      base.username ||
      base.password ||
      base.search ||
      base.hash
    ) {
      return null;
    }

    if (!urlPath) return base.toString().replace(/\/$/, "");
    if (!urlPath.startsWith("/") || urlPath.startsWith("//")) return null;

    const target = new URL(
      urlPath.replace(/^\/+/, ""),
      `${base.origin}${base.pathname.replace(/\/?$/, "/")}`,
    );
    return target.origin === base.origin ? target.toString() : null;
  } catch {
    return null;
  }
}

/**
 * Resolve only explicit, bounded manifest locations.
 *
 * The previous `__dirname/../../..` and `$HOME/robothor` search caused Next's
 * output tracer to conservatively copy the whole application tree into the
 * production standalone image. Operators can provide either the exact file or
 * the workspace root; the container deployment normally uses service URL
 * environment overrides and does not need a filesystem manifest.
 */
function manifestPaths(): string[] {
  const paths = [
    path.join(
      /* turbopackIgnore: true */ process.cwd(),
      "robothor-services.json"
    ),
  ];
  const explicitManifest = process.env.ROBOTHOR_SERVICES_MANIFEST;
  const workspace = process.env.ROBOTHOR_WORKSPACE;

  if (explicitManifest) {
    paths.unshift(path.resolve(/* turbopackIgnore: true */ explicitManifest));
  }
  if (workspace) {
    paths.push(
      path.join(/* turbopackIgnore: true */ workspace, "robothor-services.json")
    );
  }
  if (process.env.NODE_ENV !== "production") {
    // Local development and tests run from app/ while the repository manifest
    // lives one directory above it. This branch is removed from production
    // builds, keeping the standalone output trace bounded to app/.
    paths.push(
      path.resolve(
        /* turbopackIgnore: true */ process.cwd(),
        "..",
        "robothor-services.json"
      )
    );
  }
  return paths;
}

let _manifest: Manifest | null = null;

function loadManifest(): Manifest {
  if (_manifest) return _manifest;

  for (const p of manifestPaths()) {
    try {
      const content = fs.readFileSync(/* turbopackIgnore: true */ p, "utf-8");
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
      const validated = validatedServiceUrl(envVal, urlPath);
      if (!validated) {
        throw new Error(`Invalid ${envKey} service URL configuration`);
      }
      return validated;
    }
  }

  const manifest = loadManifest();
  const svc = manifest.services[name];
  if (!svc) return null;

  const protocol = svc.protocol ?? "http";
  if (protocol !== "http" && protocol !== "https") return null;
  const base = `${protocol}://${svc.host}:${svc.port}`;
  return validatedServiceUrl(base, urlPath);
}

const DEFAULT_ENGINE_URL = "http://127.0.0.1:18800";
// A Kubernetes Service name segment: what the chart's `{tenant}` template can
// actually name. Anything else would be spliced into a URL as-is.
const TENANT_ID = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

function rootTenant(): string {
  return process.env.ROBOTHOR_DEFAULT_TENANT?.trim() || "default";
}

function tenantEngineOverride(tenant: string): string | undefined {
  const raw = process.env.GENUS_TENANT_ENGINE_URLS?.trim();
  if (!raw) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("Invalid GENUS_TENANT_ENGINE_URLS: not a JSON object");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Invalid GENUS_TENANT_ENGINE_URLS: not a JSON object");
  }
  const value = (parsed as Record<string, unknown>)[tenant];
  return typeof value === "string" && value ? value : undefined;
}

/**
 * The engine that serves `tenant`, resolved in the bridge's order:
 * `GENUS_TENANT_ENGINE_URLS` (JSON `{tenant: url}`), then
 * `GENUS_TENANT_ENGINE_URL_TEMPLATE` (literal `{tenant}` placeholder), then
 * `ROBOTHOR_ENGINE_URL`. The root tenant (`ROBOTHOR_DEFAULT_TENANT`, default
 * `default`) — and no tenant at all — is always `ROBOTHOR_ENGINE_URL`.
 *
 * Read at call time, and fails closed: a misconfigured override or template
 * throws rather than sending a child tenant's session to the root engine.
 */
export function engineUrlFor(tenant?: string | null): string {
  const root = (process.env.ROBOTHOR_ENGINE_URL || DEFAULT_ENGINE_URL).replace(/\/$/, "");
  if (!tenant || tenant === rootTenant()) return root;

  const override = tenantEngineOverride(tenant);
  const template = process.env.GENUS_TENANT_ENGINE_URL_TEMPLATE?.trim();
  let candidate: string;
  if (override) {
    candidate = override;
  } else if (template?.includes("{tenant}")) {
    if (!TENANT_ID.test(tenant)) throw new Error("Invalid tenant id for engine routing");
    candidate = template.replaceAll("{tenant}", tenant);
  } else {
    return root;
  }
  const validated = validatedServiceUrl(candidate);
  if (!validated) throw new Error(`Invalid engine URL configuration for tenant ${tenant}`);
  return validated;
}

/** Resolve a health/readiness target from operator runtime configuration only. */
export function getConfiguredServiceUrl(name: string, urlPath = ""): string | null {
  const envKey = ENV_OVERRIDES[name];
  if (!envKey) return null;
  const value = process.env[envKey];
  return value ? validatedServiceUrl(value, urlPath) : null;
}

/**
 * Get the health check URL for a service.
 */
/** Name of the env var that overrides a service's URL, if one exists. */
export function serviceEnvVar(name: string): string | null {
  return ENV_OVERRIDES[name] ?? null;
}

/**
 * The path a service publishes its health/readiness on, from the manifest.
 * Engine and bridge keep `/health` behind their auth wall and publish an
 * unauthenticated `/ready` with per-dependency checks, so that is the default
 * for both when the manifest is absent.
 */
const DEFAULT_HEALTH_PATHS: Record<string, string> = {
  engine: "/ready",
  bridge: "/ready",
};

export function getServiceHealthPath(name: string): string {
  const svc = loadManifest().services[name];
  return svc?.health ?? DEFAULT_HEALTH_PATHS[name] ?? "/health";
}

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
