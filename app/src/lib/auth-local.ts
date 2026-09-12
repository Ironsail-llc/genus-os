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
  user: {
    id: string;
    email: string;
    display_name: string;
    role: string;
    tenant_id: string;
  };
};

/**
 * The set of strings pydantic v2 reads as booleans, which is what the bridge
 * parses `GENUS_LOCAL_LOGIN` with (`AuthSettings.local_login`).
 *
 * This used to require exactly `"true"`. The two sides then disagreed on `1`,
 * `yes`, `on`, `t`, `y` and `TRUE`: the bridge published the public password
 * endpoint while the dashboard registered no provider and rendered no form —
 * the surface without the UI, which is the wrong half to fail open. Anything
 * outside both sets stays off here (the bridge raises instead, which is also a
 * refusal, and nothing in between is treated as consent).
 */
const TRUTHY = new Set(["1", "on", "t", "true", "y", "yes"]);

export function localLoginEnabled(): boolean {
  return TRUTHY.has((process.env.GENUS_LOCAL_LOGIN || "").trim().toLowerCase());
}

/**
 * Whether the BRIDGE is currently offering email + password sign-in.
 *
 * `localLoginEnabled()` above reads this process's environment, and that answer
 * is fixed when the dashboard boots. On a fresh appliance that is the wrong
 * authority: the first-run wizard turns local login on for the instance —
 * writing it to `config.yaml`, which the dashboard does not read — so a
 * boot-time constant said "off" for the whole of first run, the `local`
 * provider was never registered, and the wizard's sign-in hand-off failed
 * until someone restarted the dashboard. A first-run flow whose documented
 * happy path ends at a restart is not a first-run flow.
 *
 * So the enablement question is asked of the bridge, per attempt, on the two
 * surfaces where it decides anything: `authorize()` and the sign-in page.
 * `GET /api/auth/methods` is public (it returns names and booleans) and it is
 * the same endpoint the sign-in page already exists to render.
 *
 * Unreachable reads as OFF. That direction costs nothing — the bridge 404s
 * `POST /api/auth/login` whenever local login is off, so a form drawn over a
 * dead endpoint helps nobody, and `bridgeLocalLogin` would return null anyway.
 * This check is belt-and-braces in front of that, never the load-bearing part.
 */
export async function bridgeLocalLoginOffered(
  bridgeUrl: string,
): Promise<boolean> {
  try {
    const res = await fetch(`${bridgeUrl}/api/auth/methods`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3_000),
    });
    if (!res.ok) return false;
    const payload: unknown = await res.json();
    return (payload as { local?: unknown } | null)?.local === true;
  } catch {
    return false;
  }
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
  // Canonical dotted quad only: a leading zero is rejected by the bridge's
  // parser (and read as octal by some), so it is not an address here either.
  return (
    parts.length === 4 &&
    parts.every((p) => /^(0|[1-9]\d{0,2})$/.test(p) && Number(p) <= 255)
  );
}

/**
 * Hops this dashboard will believe, from `GENUS_DASHBOARD_TRUSTED_PROXIES`.
 *
 * Same syntax as the bridge's `GENUS_TRUSTED_PROXIES`: comma-separated
 * addresses or CIDR ranges. Empty — the default — trusts nothing, and then no
 * `X-Client-IP` is asserted at all and the bridge falls back to its peer
 * address. A deployment that has not thought about its edge must not be
 * quietly asserting an address it cannot vouch for.
 */
export function dashboardTrustedProxies(): string[] {
  return (process.env.GENUS_DASHBOARD_TRUSTED_PROXIES || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function ipv4Bytes(value: string): number[] | null {
  const parts = value.split(".");
  if (parts.length !== 4) return null;
  const bytes: number[] = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    bytes.push(octet);
  }
  return bytes;
}

/** An address as its bytes (4 for IPv4, 16 for IPv6), or null if it is not one. */
function ipBytes(value: string): number[] | null {
  if (!value.includes(":")) return ipv4Bytes(value);
  const halves = value.split("::");
  if (halves.length > 2) return null;
  const expand = (half: string | undefined): number[] | null => {
    if (!half) return [];
    const groups = half.split(":");
    const bytes: number[] = [];
    for (let i = 0; i < groups.length; i += 1) {
      const group = groups[i];
      if (group.includes(".")) {
        // The IPv4-mapped tail, which is only legal last.
        if (i !== groups.length - 1) return null;
        const mapped = ipv4Bytes(group);
        if (!mapped) return null;
        bytes.push(...mapped);
        continue;
      }
      if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return null;
      const word = parseInt(group, 16);
      bytes.push(word >> 8, word & 0xff);
    }
    return bytes;
  };
  const left = expand(halves[0]);
  if (!left) return null;
  if (halves.length === 1) return left.length === 16 ? left : null;
  const right = expand(halves[1]);
  if (!right) return null;
  const zeros = 16 - left.length - right.length;
  if (zeros < 0) return null;
  return [...left, ...new Array<number>(zeros).fill(0), ...right];
}

function inNetwork(address: number[], entry: string): boolean {
  const [rawNetwork, rawPrefix] = entry.split("/");
  const network = ipBytes(rawNetwork.trim());
  if (!network || network.length !== address.length) return false;
  let bits = network.length * 8;
  if (rawPrefix !== undefined) {
    // An empty or non-numeric prefix must not read as /0, which would match
    // every address in the world.
    if (!/^\d{1,3}$/.test(rawPrefix.trim())) return false;
    bits = Number(rawPrefix.trim());
    if (bits > network.length * 8) return false;
  }
  for (let i = 0; i < network.length && bits > 0; i += 1) {
    const take = Math.min(8, bits);
    const mask = (0xff << (8 - take)) & 0xff;
    if ((address[i] & mask) !== (network[i] & mask)) return false;
    bits -= take;
  }
  return true;
}

/** Whether *value* is one of the hops this deployment trusts. */
export function isTrustedProxy(
  value: string,
  entries = dashboardTrustedProxies(),
): boolean {
  const address = ipBytes(value);
  if (!address) return false;
  return entries.some((entry) => inNetwork(address, entry));
}

/**
 * The end user's address, for the bridge's limiter and audit trail.
 *
 * `x-forwarded-for` is a list, and its LEFT-most entry is the one position in
 * the header a client can write: every appending proxy — nginx with
 * `proxy_add_x_forwarded_for`, ingress-nginx with `use-forwarded-headers`,
 * Cloudflare — produces `<whatever the client sent>, <the real client>`. Taking
 * `[0]`, as this did, handed the attacker the limiter key and the audit
 * subject: a fresh fabricated address per request is a fresh quota, and
 * `user_sessions.ip` plus every `auth.login` row then records an address of the
 * attacker's choosing, which can be pointed at an innocent third party.
 *
 * So: walk the list from the RIGHT and return the first hop that is NOT a
 * trusted proxy — uvicorn's own `ProxyHeadersMiddleware` algorithm, and the
 * mirror image of the bridge's `_peer_is_trusted`. With no trusted proxies
 * configured nothing is asserted at all; if every hop is trusted the list
 * carries no client to report; and junk to the right of the client means the
 * edge is not what this deployment thinks it is, so nothing is asserted then
 * either.
 */
export function clientIpFromRequest(
  request: Request | undefined,
): string | null {
  const headers = request?.headers;
  if (!headers) return null;
  const trusted = dashboardTrustedProxies();
  if (trusted.length === 0) return null;

  const hops = (headers.get("x-forwarded-for") || "")
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);
  for (let i = hops.length - 1; i >= 0; i -= 1) {
    const hop = hops[i];
    // The hop must be a real address, not merely address-shaped: what is
    // returned here is forwarded verbatim, and the bridge discards anything
    // its parser rejects, which would silently degrade the limiter.
    if (!looksLikeIpAddress(hop) || ipBytes(hop) === null) return null;
    if (!isTrustedProxy(hop, trusted)) return hop;
  }
  // Every hop was a trusted proxy, or there was no forwarded list at all.
  // There is no header the dashboard can verify on its own in either case
  // (`x-real-ip` says nothing about who wrote it), so vouch for nothing and
  // let the bridge use its peer address.
  return null;
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

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  // The bridge's per-IP limiter is otherwise inert: this call is made
  // SERVER-side, so request.client.host there is the dashboard pod for every
  // sign-in on the planet — one global bucket, in which five attempts from
  // anywhere lock every user of an address out, and a distributed spray is
  // not slowed at all. Two allowlists have to agree before the value is
  // believed: the bridge honours the header only from a peer named in
  // GENUS_TRUSTED_PROXIES (loopback included, never implicit), and
  // `clientIpFromRequest` only produces one at all when this deployment names
  // its own edge in GENUS_DASHBOARD_TRUSTED_PROXIES. Unset, nothing is sent
  // and the bridge uses its peer address.
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
