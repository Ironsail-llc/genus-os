import { serviceEnvVar } from "./registry";

export type ServiceStatus = "healthy" | "degraded" | "unhealthy" | "disabled";

export interface ServiceHealth {
  name: string;
  /** Operator-facing name; the internal service id means nothing on a dashboard. */
  label: string;
  url: string;
  status: ServiceStatus;
  responseTime: number;
  /** Why the status is what it is: passing/failing checks, "switched off", the HTTP code. */
  detail: string;
}

/** The services the Helm probes for the home screen and /api/health, in display order. */
export const CORE_SERVICES = ["engine", "bridge", "orchestrator", "vision"] as const;

const PROBED_SERVICES = new Set<string>(CORE_SERVICES);

const SERVICE_LABELS: Record<string, string> = {
  engine: "Agent engine",
  bridge: "API bridge",
  orchestrator: "Memory & retrieval",
  vision: "Vision (camera)",
};

export function serviceLabel(name: string): string {
  return SERVICE_LABELS[name] ?? name;
}

/** "ok" unless a service that is meant to be running is not healthy. */
export function overallStatus(services: ReadonlyArray<Pick<ServiceHealth, "status">>): "ok" | "degraded" {
  return services.every((s) => s.status === "healthy" || s.status === "disabled") ? "ok" : "degraded";
}

type ProbeBody = {
  status?: unknown;
  checks?: unknown;
  services?: unknown;
  components?: unknown;
  mode?: unknown;
  available?: unknown;
  running?: unknown;
};

/** `{name: "ok" | "error:…"}` maps (engine/bridge `checks`, bridge `/health` `services`). */
function stringChecks(value: unknown): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entries = Object.entries(value as Record<string, unknown>).filter(
    ([, v]) => typeof v === "string",
  ) as [string, string][];
  return entries.length ? Object.fromEntries(entries) : null;
}

/** `{name: {available: boolean}}` maps (orchestrator `components`). */
function availabilityChecks(value: unknown): Record<string, string> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const entries: [string, string][] = [];
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (v && typeof v === "object" && "available" in v) {
      entries.push([k, (v as { available?: unknown }).available === false ? "unavailable" : "ok"]);
    }
  }
  return entries.length ? Object.fromEntries(entries) : null;
}

/**
 * Read what the service SAID, not just whether the socket answered.
 *
 * - a body that reports itself switched off (`mode: "disabled"`, or
 *   `running` without `available`) is "disabled": neither healthy nor down;
 * - a failing dependency check is "degraded" and the check is named;
 * - an authentication wall on the probe path is named as such — it is a
 *   misconfigured probe, not an outage (2026-09-11: the bridge showed
 *   "unhealthy" on the home screen because /health answered 401 while the
 *   operator had just signed in through it).
 */
export function classifyProbe(
  httpStatus: number,
  body: unknown,
): { status: ServiceStatus; detail: string } {
  const b = (body && typeof body === "object" ? body : {}) as ProbeBody;
  if (b.mode === "disabled" || (b.running === true && b.available === false)) {
    const mode = typeof b.mode === "string" ? b.mode : "disabled";
    return { status: "disabled", detail: `switched off (mode: ${mode})` };
  }
  if (httpStatus === 401 || httpStatus === 403) {
    return { status: "unhealthy", detail: `HTTP ${httpStatus}: the probe path requires authentication` };
  }
  const checks = stringChecks(b.checks) ?? stringChecks(b.services) ?? availabilityChecks(b.components);
  const failing = checks ? Object.entries(checks).filter(([, v]) => v !== "ok") : [];
  if (failing.length > 0) {
    return { status: "degraded", detail: `failing: ${failing.map(([k, v]) => `${k} ${v}`).join(", ")}` };
  }
  const ok = httpStatus >= 200 && httpStatus < 300;
  if (b.status === "degraded") {
    return { status: "degraded", detail: ok ? "service reports degraded" : `service reports degraded (HTTP ${httpStatus})` };
  }
  if (!ok) return { status: "unhealthy", detail: `HTTP ${httpStatus}` };
  const passed = checks ? Object.keys(checks) : [];
  return {
    status: "healthy",
    detail: passed.length ? `checks: ${passed.map((k) => `${k} ok`).join(", ")}` : "responding",
  };
}

/** Validate an operator-owned service target before any network access. */
function healthTarget(name: string, value: string): URL | null {
  if (!PROBED_SERVICES.has(name) || value.length > 2_048) return null;

  try {
    const target = new URL(value);
    if (
      !["http:", "https:"].includes(target.protocol) ||
      !target.hostname ||
      target.username ||
      target.password ||
      target.hash
    ) {
      return null;
    }
    return target;
  } catch {
    return null;
  }
}

/**
 * Is at least one sign-in path fully configured?
 *
 * Mirrors `oidcProviderConfigured()` in src/lib/auth.ts:280, which is the
 * canonical definition. Deliberately NOT imported: auth.ts builds the NextAuth
 * provider array at module scope, and /api/ready must not drag NextAuth into a
 * readiness probe. `authProvidersInSync` in the tests pins the two together so
 * this copy cannot drift.
 */
function oidcConfiguredLocally(): boolean {
  return Boolean(
    process.env.AUTH_OIDC_ISSUER?.trim() && process.env.AUTH_OIDC_CLIENT_ID?.trim(),
  );
}

/** Cloudflare Access, mirroring `cfAccessEnabled()` in src/lib/cf-access.ts:37. */
function cfAccessConfiguredLocally(): boolean {
  return Boolean(
    process.env.CF_ACCESS_TEAM_DOMAIN?.trim() && process.env.CF_ACCESS_AUD?.trim(),
  );
}

/** Verify the dashboard can authenticate users without exposing config values.
 *
 * Previously this demanded the OIDC triple unconditionally, so a box that signs
 * in exclusively through Cloudflare Access reported `authentication: unhealthy`
 * while authenticating users perfectly well. That is a FALSE NEGATIVE in a
 * readiness probe, and it is worse than no check: it made /api/ready report
 * `degraded` permanently, which is what taught everyone to ignore it while the
 * bridge was genuinely 403ing every sign-in.
 *
 * auth.ts registers the two providers independently (`:287` OIDC, `:49`
 * Cloudflare), so either path alone is a working deployment.
 */
export function checkDashboardAuthConfig(): ServiceHealth {
  const environment = (
    process.env.GENUS_ENVIRONMENT ??
    process.env.ROBOTHOR_ENVIRONMENT ??
    ""
  ).toLowerCase();
  const insecureDevelopment =
    process.env.GENUS_INSECURE_DEV_MODE === "true" &&
    environment !== "production" &&
    environment !== "prod";

  // Needed by BOTH paths: AUTH_SECRET signs the session, and the SSO secret is
  // what the bridge exchange authenticates with. Without the latter every
  // sign-in 403s no matter which IdP verified the user.
  const common = ["AUTH_SECRET", "GENUS_BRIDGE_SSO_SECRET"].every((name) =>
    Boolean(process.env[name]?.trim()),
  );
  const aProviderWorks = oidcConfiguredLocally() || cfAccessConfiguredLocally();
  const configured = insecureDevelopment || (common && aProviderWorks);

  return {
    name: "authentication",
    label: "Sign-in configuration",
    url: "local",
    status: configured ? "healthy" : "unhealthy",
    responseTime: 0,
    detail: configured
      ? insecureDevelopment
        ? "insecure development mode"
        : "shared secrets set and a sign-in provider configured"
      : "AUTH_SECRET, GENUS_BRIDGE_SSO_SECRET and one sign-in provider (OIDC or Cloudflare Access) are required",
  };
}

/** Check one backend without allowing an absent URL to look healthy. */
export async function checkService(
  name: string,
  url: string | null
): Promise<ServiceHealth> {
  const start = Date.now();
  const label = serviceLabel(name);
  if (!url) {
    const envVar = serviceEnvVar(name);
    // A variable that is SET but unusable (wrong scheme, credentials, a
    // fragment) is a broken deployment, not an absent service.
    if (envVar && process.env[envVar]?.trim()) {
      return {
        name,
        label,
        url: "invalid",
        status: "unhealthy",
        responseTime: 0,
        detail: `${envVar} is not a usable http(s) URL`,
      };
    }
    return {
      name,
      label,
      url: "unconfigured",
      status: "disabled",
      responseTime: 0,
      detail: envVar ? `not configured (set ${envVar})` : "not configured",
    };
  }

  const target = healthTarget(name, url);
  if (!target) {
    return { name, label, url: "invalid", status: "unhealthy", responseTime: 0, detail: "invalid probe URL" };
  }

  try {
    const res = await fetch(target, {
      redirect: "manual",
      signal: AbortSignal.timeout(5000),
    });
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    // A Response always carries a numeric status; test doubles sometimes only say `ok`.
    const httpStatus = typeof res.status === "number" ? res.status : res.ok ? 200 : 503;
    const verdict = classifyProbe(httpStatus, body);
    return {
      name,
      label,
      url: target.toString(),
      status: verdict.status,
      responseTime: Date.now() - start,
      detail: verdict.detail,
    };
  } catch (err) {
    const reason = err instanceof Error ? err.name === "TimeoutError" ? "timed out after 5s" : err.message : String(err);
    return {
      name,
      label,
      url: target.toString(),
      status: "unhealthy",
      responseTime: Date.now() - start,
      detail: `unreachable: ${reason}`,
    };
  }
}
