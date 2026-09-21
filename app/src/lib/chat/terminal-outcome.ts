/** A lost response is not permission to repeat a possibly dispatched action. */
export const OUTCOME_UNKNOWN =
  "Connection interrupted. Checking the recorded result…";

export function terminalOutcome(
  data: { text?: unknown; status?: unknown; aborted?: unknown; audit_outcome?: unknown },
  partial: string,
): string | undefined {
  // Only the host's scoped, settled audit projection can replace recovery for an
  // interrupted run. Model prose alone remains insufficient.
  if (data.audit_outcome === true && typeof data.text === "string" && data.text) {
    return data.text;
  }
  if (data.aborted === true) return OUTCOME_UNKNOWN;
  if (["failed", "timeout", "cancelled"].includes(String(data.status))) {
    // A terminal execution error does not establish the outcome of dispatched
    // actions. Recover the durable failure and its receipts using the same request.
    return OUTCOME_UNKNOWN;
  }
  if (typeof data.text !== "string") return undefined;
  // Legacy streams use empty done text after an error event. Preserve that error.
  return data.text || (data.status === "completed" ? "" : partial);
}


/** Only an explicit pre-dispatch refusal can discard the pending request. */
export async function requestFailure(response: Response): Promise<{ text: string; rejected: boolean }> {
  try {
    const body = await response.json();
    const text = typeof body?.error === "string" && body.error ? body.error : OUTCOME_UNKNOWN;
    return { text, rejected: response.ok === false && body?.request_admitted === false && text !== OUTCOME_UNKNOWN };
  } catch {
    return { text: OUTCOME_UNKNOWN, rejected: false };
  }
}
