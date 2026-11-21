/**
 * Server-side: does this instance serve workspaces, for this caller?
 *
 * Asked the way the browser's shell asks (`GET /api/rooms`), with the
 * caller's own bridge credentials. Only a 200 is "yes". A 404 (nothing serves
 * rooms here), any other refusal, or no answer is "no": the caller is then
 * told nothing about workspaces rather than about tools it may not have.
 */
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";

export async function roomsServed(): Promise<boolean> {
  try {
    const url = getServiceUrl("bridge", "/api/rooms") || "http://localhost:9100/api/rooms";
    const res = await fetch(url, {
      headers: await bridgeAuthHeaders(),
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    return res.ok;
  } catch {
    return false;
  }
}
