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

/** Verify the dashboard can authenticate users without exposing config values. */
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
  const required = [
    "AUTH_SECRET",
    "AUTH_OIDC_ISSUER",
    "AUTH_OIDC_CLIENT_ID",
    "AUTH_OIDC_CLIENT_SECRET",
    "GENUS_BRIDGE_SSO_SECRET",
  ];
  const configured =
    insecureDevelopment || required.every((name) => Boolean(process.env[name]?.trim()));
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
