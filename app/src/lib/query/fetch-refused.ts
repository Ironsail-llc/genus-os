/**
 * A listing read that got a refusal, or no answer at all (status 0: offline,
 * connection refused). Thrown out of a query so the hook can report
 * `{ endpoint, status }` instead of showing an empty list as if it were true.
 */
export class FetchRefused extends Error {
  constructor(
    readonly endpoint: string,
    readonly status: number
  ) {
    super(`${endpoint} answered ${status}`);
  }
}

/** The `{ endpoint, status }` a hook returns as `error`, or `null`. */
export function refusalOf(error: unknown): { endpoint: string; status: number } | null {
  return error instanceof FetchRefused ? { endpoint: error.endpoint, status: error.status } : null;
}

/** POST one tool through the actions route; a refusal or a lost request throws FetchRefused. */
export async function executeTool(tool: string, params: Record<string, unknown>, signal: AbortSignal) {
  const endpoint = "/api/actions/execute";
  let res: Response;
  try {
    res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tool, params }),
      signal,
    });
  } catch (err) {
    if (signal.aborted) throw err;
    throw new FetchRefused(endpoint, 0);
  }
  if (!res.ok) throw new FetchRefused(endpoint, res.status);
  return res.json();
}
