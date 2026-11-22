/**
 * A workspace email (`room.email`): what a member approves in a task
 * workspace, and the receipt the engine writes after the decision.
 *
 * The approval row's `email` is the exact email the approval authorizes
 * (`crm/bridge/routers/approvals.py`, `_room_email`); the receipt is the
 * `receipt` of an assistant turn whose `kind` is `room_email_receipt`
 * (`robothor/rooms/outbound.py`).
 */

/** The email, field by field, exactly as it would be sent. */
export interface RoomEmail {
  from: string;
  replyTo: string;
  to: string[];
  cc: string[];
  subject: string;
  body: string;
  /** Whose request it is (the member who approves it). */
  onBehalfOf: string;
}

export type ReceiptStatus = "sent" | "failed" | "uncertain" | "not_sent" | "rejected" | "expired";

const STATUSES: readonly ReceiptStatus[] = ["sent", "failed", "uncertain", "not_sent", "rejected", "expired"];

/** What happened to an email a member was asked to approve. */
export interface RoomEmailReceipt {
  status: ReceiptStatus;
  from: string;
  replyTo: string;
  to: string[];
  cc: string[];
  subject: string;
  /** Who decided it, by name. */
  decidedBy: string | null;
  sentAt: string | null;
  /** Why it was not sent, when it was not. */
  error: string | null;
}

export const RECEIPT_KIND = "room_email_receipt";

const str = (v: unknown) => (typeof v === "string" ? v : "");
const list = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

function person(v: unknown): string | null {
  if (typeof v === "string" && v) return v;
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    const name = str(o.display_name) || str(o.displayName);
    return name || null;
  }
  return null;
}

/** The exact email of an approval row, or null when it is not one. */
export function readEmail(raw: unknown): RoomEmail | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  if (!str(o.from) || !Array.isArray(o.to)) return null;
  return {
    from: str(o.from),
    replyTo: str(o.replyTo),
    to: list(o.to),
    cc: list(o.cc),
    subject: str(o.subject),
    body: str(o.body),
    onBehalfOf: str(o.onBehalfOf),
  };
}

/** A receipt turn's receipt, or null when it is not a receipt this screen knows. */
export function readEmailReceipt(raw: unknown): RoomEmailReceipt | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const status = STATUSES.find((s) => s === o.status);
  if (!status) return null;
  return {
    status,
    from: str(o.from),
    replyTo: str(o.reply_to) || str(o.replyTo),
    to: list(o.to),
    cc: list(o.cc),
    subject: str(o.subject),
    decidedBy: person(o.decided_by) ?? person(o.approved_by),
    sentAt: str(o.sent_at) || null,
    error: str(o.error) || null,
  };
}
