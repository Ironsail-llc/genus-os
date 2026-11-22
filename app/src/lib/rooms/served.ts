/**
 * Server-side: does this instance serve workspaces, for this caller?
 *
 * Asked the way the browser's shell asks (`GET /api/rooms`), with the
 * caller's own bridge credentials. Only two answers are definite: a 200 is
 * "served" and a 404 (nothing serves rooms here) is "not_served". Anything
 * else (a timeout, no answer, another refusal) is "unknown": the caller treats
 * it as not served for that request and never remembers it.
 */
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";

export type RoomsServed = "served" | "not_served" | "unknown";

export async function roomsServed(): Promise<RoomsServed> {
  try {
    const url = getServiceUrl("bridge", "/api/rooms") || "http://localhost:9100/api/rooms";
    const res = await fetch(url, {
      headers: await bridgeAuthHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    if (res.ok) return "served";
    return res.status === 404 ? "not_served" : "unknown";
  } catch {
    return "unknown";
  }
}
