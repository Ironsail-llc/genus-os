/** Number formats the Runs page and its detail drawer share. */

export function formatTokens(n: number): string {
  return n.toLocaleString("en-US");
}

/** A cost in dollars, or null when the engine recorded none. */
export function formatCost(usd: number | null | undefined, digits = 4): string | null {
  return typeof usd === "number" && Number.isFinite(usd) ? `$${usd.toFixed(digits)}` : null;
}
