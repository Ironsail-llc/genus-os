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
): Promise<LocalLoginResult | "mfa_required" | null> {
  const body: Record<string, string> = {
    email: credentials.email,
    password: credentials.password,
  };
  // An empty string is not a code. Sending one would spend a lockout strike
  // on a user who simply has not been asked for it yet.
  if (credentials.code) body.code = credentials.code;

  let res: Response;
  try {
    res = await fetch(`${bridgeUrl}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
