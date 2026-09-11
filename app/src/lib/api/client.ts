const BASE = typeof window !== "undefined" ? "" : "http://localhost:3004";

/**
 * A BFF call that came back non-2xx.
 *
 * Carries the endpoint and status so a view can tell the operator *what*
 * failed and *how* ("Conversations unavailable (401)") instead of rendering an
 * empty card. The message is unchanged from the plain Error it replaces, so
 * existing `String(err)` call sites read the same.
 */
export class ApiError extends Error {
  readonly endpoint: string;
  readonly status: number;

  constructor(endpoint: string, status: number, statusText: string) {
    super(`API error ${status}: ${statusText}`);
    this.name = "ApiError";
    this.endpoint = endpoint;
    this.status = status;
  }
}

export async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
    ...options,
  });

  if (!res.ok) {
    throw new ApiError(path, res.status, res.statusText);
  }

  return res.json();
}
