/**
 * Client-side error reporting endpoint.
 * Receives errors from the dashboard agent hook and iframe chart rendering,
 * and logs them to stdout (→ journalctl) so they're visible in server logs.
 */

const RATE_LIMIT_WINDOW = 60_000;
const RATE_LIMIT_MAX = 30;
const errorLog: number[] = [];

const oneLine = (s: unknown) => String(s).replace(/[\r\n\u2028\u2029]/g, " ");

export async function POST(req: Request) {
  // Simple rate limit to prevent log spam
  const now = Date.now();
  const cutoff = now - RATE_LIMIT_WINDOW;
  const firstValid = errorLog.findIndex((t) => t >= cutoff);
  if (firstValid > 0) errorLog.splice(0, firstValid);
  else if (firstValid === -1) errorLog.length = 0;
  if (errorLog.length >= RATE_LIMIT_MAX) {
    return new Response(null, { status: 429 });
  }
  errorLog.push(now);

  try {
    const body = await req.json();
    const source = String(body.source || "unknown").slice(0, 50);
    const message = String(body.message || "").slice(0, 500);
    const details = body.details ? JSON.stringify(body.details).slice(0, 1000) : "";
    const status = body.status ? String(body.status) : "";

    // Log to stdout → journalctl with structured prefix for easy grep
    console.error(
      `[dashboard-error] source=${oneLine(source)}${status ? ` status=${oneLine(status)}` : ""} | ${oneLine(message)}${details ? ` | ${oneLine(details)}` : ""}`
    );

    return new Response(null, { status: 204 });
  } catch {
    return new Response(null, { status: 400 });
  }
}
