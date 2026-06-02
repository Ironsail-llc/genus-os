import { auth } from "@/lib/auth";
import { HELM_AGENT_ID } from "@/lib/config";

/**
 * Server-side identity headers for backend (bridge/orchestrator/vision) calls.
 *
 * Prefers the verified bridge access token from the Auth.js session
 * (``Authorization: Bearer``). Falls back to the legacy ``X-Agent-Id`` when
 * there's no session — so unauthenticated/dev requests keep working while the
 * bridge runs in shadow mode (GENUS_AUTH_ENFORCE off). Once enforcement is on,
 * unauthenticated requests never reach here (the middleware redirects to /signin).
 */
export async function bridgeAuthHeaders(): Promise<Record<string, string>> {
  try {
    const session = await auth();
    if (session?.bridgeAccess) {
      return { Authorization: `Bearer ${session.bridgeAccess}` };
    }
  } catch {
    // fall through to the legacy header
  }
  return { "X-Agent-Id": HELM_AGENT_ID };
}
