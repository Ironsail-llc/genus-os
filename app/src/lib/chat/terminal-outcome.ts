/** A lost response is not permission to repeat a possibly dispatched action. */
export const OUTCOME_UNKNOWN =
  "Connection interrupted. Checking the recorded result…";

export function terminalOutcome(
  data: { text?: unknown; status?: unknown; aborted?: unknown },
  partial: string,
): string | undefined {
  if (data.aborted === true) return OUTCOME_UNKNOWN;
  if (["failed", "timeout", "cancelled"].includes(String(data.status))) {
    return typeof data.text === "string" && data.text ? data.text : OUTCOME_UNKNOWN;
  }
  if (typeof data.text !== "string") return undefined;
  // Legacy streams use empty done text after an error event. Preserve that error.
  return data.text || (data.status === "completed" ? "" : partial);
}
