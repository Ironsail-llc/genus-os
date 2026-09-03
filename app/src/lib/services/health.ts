export interface ServiceHealth {
  name: string;
  url: string;
  status: "healthy" | "unhealthy";
  responseTime: number;
}

const PROBED_SERVICES = new Set(["engine", "bridge", "orchestrator", "vision"]);

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
    url: "local",
    status: configured ? "healthy" : "unhealthy",
    responseTime: 0,
  };
}

/** Check one backend without allowing an absent URL to look healthy. */
export async function checkService(
  name: string,
  url: string | null
): Promise<ServiceHealth> {
  const start = Date.now();
  if (!url) {
    return { name, url: "unconfigured", status: "unhealthy", responseTime: 0 };
  }

  const target = healthTarget(name, url);
  if (!target) {
    return { name, url: "invalid", status: "unhealthy", responseTime: 0 };
  }

  try {
    const res = await fetch(target, {
      redirect: "manual",
      signal: AbortSignal.timeout(5000),
    });
    return {
      name,
      url: target.toString(),
      status: res.ok ? "healthy" : "unhealthy",
      responseTime: Date.now() - start,
    };
  } catch {
    return {
      name,
      url: target.toString(),
      status: "unhealthy",
      responseTime: Date.now() - start,
    };
  }
}
