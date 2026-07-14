import { auth } from "@/lib/auth";
import { HELM_AGENT_ID } from "@/lib/config";

/**
 * Server-side identity headers for backend (bridge/orchestrator/vision) calls.
 *
 * Prefers the verified bridge access token from the Auth.js session
 * (``Authorization: Bearer``). Legacy identity headers are emitted only in an
 * explicitly insecure non-production development runtime. In every other
 * environment a missing session produces no credential, so the bridge rejects
 * the request rather than trusting a caller-controlled identity header.
 */
export async function bridgeAuthHeaders(): Promise<Record<string, string>> {
  try {
    const session = await auth();
    if (session?.user && session.bridgeAccess && !session.authError) {
      return { Authorization: `Bearer ${session.bridgeAccess}` };
    }
  } catch {
    // Treat session lookup failures as unauthenticated.
  }

  const environment = (
    process.env.GENUS_ENVIRONMENT ??
    process.env.ROBOTHOR_ENVIRONMENT ??
    ""
  ).toLowerCase();
  if (
    process.env.GENUS_INSECURE_DEV_MODE === "true" &&
    environment !== "production" &&
    environment !== "prod"
  ) {
    return { "X-Agent-Id": HELM_AGENT_ID };
  }
  return {};
}
