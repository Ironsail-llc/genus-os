/**
 * Local email + password sign-in, dashboard side.
 *
 * The bridge is still the token authority: `authorize()` posts the credentials
 * to `POST /api/auth/login`, and what comes back is the SAME access/refresh
 * pair the SSO exchange mints. There is no second exchange afterwards — see
 * the `local` branch of `bridgeJwtCallback`.
 *
 * Kept out of `auth.ts` so the type augmentation in `types/next-auth.d.ts` can
 * import `LocalLoginResult` without importing the NextAuth config itself.
 */

export type LocalLoginResult = {
  access_token: string;
  refresh_token: string;
  mfa_setup_required?: boolean;
  user: { id: string; email: string; display_name: string; role: string; tenant_id: string };
};

/**
 * Exactly `"true"`, never a general truthiness test: a password endpoint that
 * switches on for anyone who set `GENUS_LOCAL_LOGIN=0` is a surprise attack
 * surface. Mirrors `robothor/auth/runtime.py::local_login_enabled` closely
 * enough for the strict opt-in, deliberately stricter for the rest.
 */
export function localLoginEnabled(): boolean {
  return process.env.GENUS_LOCAL_LOGIN === "true";
}

/**
 * IPv4 dotted quad, or anything shaped like IPv6. Deliberately loose — the
 * bridge re-validates with a real parser before trusting the value; this only
 * has to stop junk and header-injection attempts from being forwarded at all.
 */
export function looksLikeIpAddress(value: string): boolean {
  if (!value || /\s/.test(value)) return false;
  if (value.includes(":")) return /^[0-9a-fA-F:.]+$/.test(value);
  const parts = value.split(".");
  return parts.length === 4 && parts.every((p) => /^\d{1,3}$/.test(p) && Number(p) <= 255);
}

/**
 * The end user's address, from the headers the edge set on THIS request.
 *
 * `x-forwarded-for` is a list and its left-most entry is the original client.
 * Returns null unless the value parses, so a header full of junk is forwarded
 * as nothing rather than as a limiter key of the sender's choosing.
 */
export function clientIpFromRequest(request: Request | undefined): string | null {
  const headers = request?.headers;
  if (!headers) return null;
  const forwarded = headers.get("x-forwarded-for") || "";
  const candidate = (forwarded.split(",")[0] || headers.get("x-real-ip") || "").trim();
  return looksLikeIpAddress(candidate) ? candidate : null;
}

export function isLocalLoginResult(value: unknown): value is LocalLoginResult {
  if (!value || typeof value !== "object") return false;
  const result = value as Partial<LocalLoginResult>;
  const user = result.user as Partial<LocalLoginResult["user"]> | undefined;
  return (
    typeof result.access_token === "string" &&
    result.access_token.length > 0 &&
    typeof result.refresh_token === "string" &&
    result.refresh_token.length > 0 &&
    !!user &&
    typeof user.id === "string" &&
    typeof user.email === "string" &&
    typeof user.display_name === "string" &&
    typeof user.role === "string" &&
    typeof user.tenant_id === "string"
  );
}

/**
 * Post one sign-in attempt to the bridge.
 *
 * Returns the issued tokens, the string `"mfa_required"` when the password was
 * right but a second factor is owed, or `null` for everything else. The three
 * outcomes mirror the bridge's own contract exactly: the dashboard must not
 * invent a distinction the bridge refused to make, so a wrong password, an
 * unknown email, a lockout, a throttle and an unreachable bridge all collapse
 * into the same `null`.
 */
export async function bridgeLocalLogin(
  bridgeUrl: string,
  credentials: { email: string; password: string; code?: string },
  clientIp?: string | null,
): Promise<LocalLoginResult | "mfa_required" | null> {
  const body: Record<string, string> = {
    email: credentials.email,
    password: credentials.password,
  };
  // An empty string is not a code. Sending one would spend a lockout strike
  // on a user who simply has not been asked for it yet.
  if (credentials.code) body.code = credentials.code;

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  // The bridge's per-IP limiter is otherwise inert: this call is made
  // SERVER-side, so request.client.host there is the dashboard pod for every
  // sign-in on the planet — one global bucket, in which five attempts from
  // anywhere lock every user of an address out, and a distributed spray is
  // not slowed at all. The bridge honours this header ONLY from a loopback
  // peer or one named in GENUS_TRUSTED_PROXIES.
  if (clientIp) headers["X-Client-IP"] = clientIp;

  let res: Response;
  try {
    res = await fetch(`${bridgeUrl}/api/auth/login`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
  } catch {
    return null;
  }

  if (res.status === 401) {
    try {
      const payload: unknown = await res.json();
      const error = (payload as { error?: string } | null)?.error;
      if (error === "mfa_required") return "mfa_required";
    } catch {
      // An unparseable 401 is still a refusal.
    }
    return null;
  }
  if (!res.ok) return null;

  try {
    const payload: unknown = await res.json();
    return isLocalLoginResult(payload) ? payload : null;
  } catch {
    return null;
  }
}
