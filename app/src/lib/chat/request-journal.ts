/** Only opaque request identifiers survive reload. Answers remain in the engine. */
const PREFIX = "helm.chat.pending.v1.";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function pendingRequests(scope: string): string[] {
  if (!UUID.test(scope)) return [];
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(PREFIX + scope) ?? "[]");
    return Array.isArray(value)
      ? [...new Set(value.filter((id): id is string => typeof id === "string" && UUID.test(id)))]
      : [];
  } catch {
    return [];
  }
}

export function rememberRequest(scope: string, requestId: string): boolean {
  if (!UUID.test(scope) || !UUID.test(requestId)) return false;
  try {
    sessionStorage.setItem(PREFIX + scope, JSON.stringify([...new Set([...pendingRequests(scope), requestId])]));
    return true;
  } catch {
    return false;
  }
}

export function forgetRequest(scope: string, requestId: string): void {
  if (!UUID.test(scope)) return;
  try {
    const remaining = pendingRequests(scope).filter((id) => id !== requestId);
    if (remaining.length) sessionStorage.setItem(PREFIX + scope, JSON.stringify(remaining));
    else sessionStorage.removeItem(PREFIX + scope);
  } catch {
    // Storage unavailability must not discard a recovered answer from chat.
  }
}
