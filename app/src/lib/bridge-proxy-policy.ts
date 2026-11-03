/**
 * Which bridge paths the BFF proxy will not forward.
 *
 * Its own module rather than a constant inside the route handler so a test can
 * import the exact pattern that ships. A copy of the regex in a test proves
 * only that the copy is correct.
 *
 * This route hands the caller's browser session to ANY bridge path, so
 * `/api/vault/get` — which used to answer an owner/admin session with a
 * decrypted credential — was reachable from the Helm, from an XSS on it, and
 * from anything holding a session cookie. The bridge now refuses human
 * sessions on that route; the browser still has no business asking, so it is
 * refused here as well. Two independent locks, because one of them was enough
 * to leak every secret in the appliance.
 *
 * `/api/setup/*` is denied for a different reason. Those routes are not
 * secret-bearing; they are the FIRST-RUN wizard, and they authenticate with a
 * short-lived setup claim token rather than a session. This proxy attaches the
 * browser's bridge token to everything it forwards, so routing the wizard
 * through it would mean the routes that create the owner account were
 * reachable with a session — the one thing that whole design refuses. The
 * wizard has its own session-free route (`/api/setup/[...path]`) that forwards
 * only the claim. The bridge refuses a session there as well; this is the
 * second lock.
 *
 * Deliberately narrow otherwise. The provider routes under `/api/providers` and
 * `/api/models` are the credential *write* path — they answer with digests and
 * verdicts, never a value — and Settings goes dark if a broader pattern ever
 * swallows them.
 *
 * `/api/auth/switch-tenant` is denied because its reply IS a bearer: the
 * bridge answers a tenant switch with a fresh access token in the body, and
 * this proxy would hand it to browser JavaScript. The Helm switches through
 * `/api/session/switch-tenant`, which keeps the token in the session cookie.
 *
 * Matched against the RESOLVED target path (after `new URL` normalizes any
 * `..` segments), never the raw string the caller supplied — and DECODED
 * first: the bridge's server percent-decodes the path before it routes, so
 * `switch%2Dtenant` reaches the switch route while failing a test against the
 * encoded string. See `normalizedBridgePath`.
 */
export const DENIED_BRIDGE_PATHS =
  /^\/(?:api\/)?(?:vault\/(?:get|list)|setup|auth\/switch-tenant)(?:\/|$)/i;

const MAX_DECODE_ROUNDS = 3;

/**
 * The path as the bridge will route it: percent-decoded until it stops
 * changing (at most three rounds, so `%252D` is caught as well as `%2D`), dot
 * segments resolved, and runs of slashes collapsed. Null when it cannot be
 * decoded, or still carries a `%` after the last round — neither is a path
 * the Helm ever sends, and both are ways to hide one.
 */
export function normalizedBridgePath(pathname: string): string | null {
  let current = pathname;
  for (let round = 0; round < MAX_DECODE_ROUNDS; round++) {
    let decoded: string;
    try {
      decoded = decodeURIComponent(current);
    } catch {
      return null;
    }
    if (decoded === current) break;
    current = decoded;
  }
  if (current.includes("%")) return null;
  return resolveSegments(current);
}

/** `/a//b/./c/../d` → `/a/b/d`: empty and dot segments as a router sees them. */
function resolveSegments(path: string): string {
  const out: string[] = [];
  for (const segment of path.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") out.pop();
    else out.push(segment);
  }
  return `/${out.join("/")}${path.endsWith("/") && out.length ? "/" : ""}`;
}

export function isDeniedBridgePath(pathname: string): boolean {
  const normalized = normalizedBridgePath(pathname);
  return normalized === null || DENIED_BRIDGE_PATHS.test(normalized);
}
