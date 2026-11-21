/**
 * One answer to `POST /api/approvals/{kind}/{id}`, read the way the Inbox
 * reads it: settled, or not and why, in the server's words. A 403 (the server
 * decided this person may not decide it) is one of those words, not a crash.
 */
import { readBridgeReply } from "@/lib/bridge/read-reply";
import type { AnswerBody, AnswerOutcome, PendingKind } from "./pending";

export const APPROVALS_ENDPOINT = "/api/bridge/api/approvals";

export async function postApprovalAnswer(id: string, kind: PendingKind, body: AnswerBody): Promise<AnswerOutcome> {
  let res: Response;
  try {
    res = await fetch(`${APPROVALS_ENDPOINT}/${kind}/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return { settled: false, message: "The dashboard could not reach the bridge to send that." };
  }

  if (!res.ok) return { settled: false, message: await readBridgeReply(res) };

  let payload: { settled?: unknown; message?: unknown } | null = null;
  try {
    payload = (await res.json()) as { settled?: unknown; message?: unknown };
  } catch {
    payload = null;
  }
  if (payload?.settled !== true) {
    return {
      settled: false,
      message:
        typeof payload?.message === "string" && payload.message ? payload.message : "The bridge did not settle that.",
    };
  }
  return { settled: true, message: null };
}
