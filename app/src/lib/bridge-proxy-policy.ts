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
 * Deliberately narrow. The provider routes under `/api/providers` and
 * `/api/models` are the credential *write* path — they answer with digests and
 * verdicts, never a value — and Settings plus the setup wizard go dark if a
 * broader pattern ever swallows them.
 *
 * Matched against the RESOLVED target path (after `new URL` normalizes any
 * `..` segments), never the raw string the caller supplied.
 */
export const DENIED_BRIDGE_PATHS = /^\/(?:api\/)?vault\/(?:get|list)(?:\/|$)/i;

export function isDeniedBridgePath(pathname: string): boolean {
  return DENIED_BRIDGE_PATHS.test(pathname);
}
