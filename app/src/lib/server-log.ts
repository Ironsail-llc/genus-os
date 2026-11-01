/**
 * Structured log lines from server routes.
 *
 * One JSON object per line on standard output, which is what a container log
 * pipeline parses into attributes. Server-only: it writes to process.stdout,
 * which a browser bundle does not have. Debug prints use neither this nor
 * console.log; a dashboard diagnostic is console.warn or console.error.
 */
export function serverLog(record: Record<string, unknown>): void {
  process.stdout.write(`${JSON.stringify(record)}\n`);
}
