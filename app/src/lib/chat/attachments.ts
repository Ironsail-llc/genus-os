/**
 * Files attached to a chat turn: the limits, shared by the composer (which
 * states and enforces them) and the send route (which enforces them again).
 * The engine checks the same three numbers (`robothor/engine/chat_attachments.py`);
 * the browser is never the boundary.
 */

/** Files one turn may carry. */
export const MAX_ATTACHMENTS = 3;

/** Bytes one file may be: 5 MB. */
export const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;

/** The types a turn may carry, by MIME, with the extension each is known by. */
export const ACCEPTED_TYPES: Record<string, string[]> = {
  "image/png": [".png"],
  "image/jpeg": [".jpg", ".jpeg"],
  "image/gif": [".gif"],
  "image/webp": [".webp"],
  "text/plain": [".txt"],
  "text/markdown": [".md", ".markdown"],
  "text/csv": [".csv"],
  "application/json": [".json"],
  "application/pdf": [".pdf"],
};

/** What the composer says, word for word, about what it takes. */
export const ATTACHMENT_LIMITS_TEXT =
  "Images (PNG, JPG, GIF, WebP) and text, Markdown, CSV, JSON or PDF · up to 5 MB each · 3 per message";

/** The `accept` attribute for the file input. */
export const ACCEPT_ATTRIBUTE = Object.entries(ACCEPTED_TYPES)
  .flatMap(([mime, exts]) => [mime, ...exts])
  .join(",");

/** One file as `/api/chat/send` carries it. */
export interface AttachmentPayload {
  name: string;
  mime: string;
  size: number;
  /** The bytes, base64. */
  data: string;
}

/** What a bubble shows about a file that went with a turn. */
export interface AttachmentSummary {
  name: string;
  size: number;
  kind: string;
}

/**
 * The MIME type to send for a file, or null when the turn cannot carry it.
 * A browser often reports no type for `.md` (or a variant like
 * `text/x-markdown`), so the extension decides when the type is missing or
 * not on the list.
 */
export function attachmentMime(name: string, type: string): string | null {
  const lowered = (type || "").toLowerCase();
  if (lowered in ACCEPTED_TYPES) return lowered;
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  for (const [mime, exts] of Object.entries(ACCEPTED_TYPES)) {
    if (exts.includes(ext)) return mime;
  }
  return null;
}

/** "5 B", "2.0 KB", "1.4 MB". */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function attachmentKind(mime: string): string {
  return mime.startsWith("image/") ? "image" : "document";
}

/** A refusal for one attachment, as `{status, error}`, or null if it may go. */
export function checkAttachment(item: unknown): { status: number; error: string } | null {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    return { status: 422, error: "each attachment must be an object" };
  }
  const { name, mime, size, data } = item as Record<string, unknown>;
  if (typeof name !== "string" || !name.trim()) return { status: 422, error: "an attachment has no name" };
  if (typeof mime !== "string" || !(mime.toLowerCase() in ACCEPTED_TYPES)) {
    return { status: 422, error: `${name} is not a supported type` };
  }
  if (typeof data !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(data) || data.length % 4 !== 0) {
    return { status: 422, error: `${name} is not valid base64` };
  }
  const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
  const decoded = (data.length / 4) * 3 - padding;
  if (decoded > MAX_ATTACHMENT_BYTES) return { status: 413, error: `${name} is larger than 5 MB` };
  if (size !== undefined && size !== decoded) {
    return { status: 422, error: `${name}'s size does not match its data` };
  }
  return null;
}

/** A refusal for the whole `attachments` field, or null. Absent is fine. */
export function checkAttachments(raw: unknown): { status: number; error: string } | null {
  if (raw === undefined || raw === null) return null;
  if (!Array.isArray(raw)) return { status: 422, error: "attachments must be a list" };
  if (raw.length > MAX_ATTACHMENTS) return { status: 413, error: `at most ${MAX_ATTACHMENTS} files per message` };
  for (const item of raw) {
    const refusal = checkAttachment(item);
    if (refusal) return refusal;
  }
  return null;
}

/** The base64 of a file's bytes. */
export async function readAsBase64(file: File): Promise<string> {
  const url = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  return url.slice(url.indexOf(",") + 1);
}

/**
 * The engine stores a turn with files as the operator's words followed by the
 * inventory note it handed the agent (`[N files attached — …]`). A bubble shows
 * the words; the files are the chips.
 */
export function stripAttachmentNote(content: string): string {
  const at = content.search(/\[\d+ files? attached — /);
  return at < 0 ? content : content.slice(0, at).trim();
}

/** The attachment rows a history entry carries, wherever the engine put them. */
export function historyAttachments(entry: unknown): AttachmentSummary[] {
  if (!entry || typeof entry !== "object") return [];
  const e = entry as { extras?: { attachments?: unknown }; attachments?: unknown };
  const rows = Array.isArray(e.extras?.attachments) ? e.extras!.attachments : e.attachments;
  if (!Array.isArray(rows)) return [];
  return rows
    .filter((r): r is Record<string, unknown> => !!r && typeof r === "object")
    .map((r) => ({
      name: String(r.original_name || r.name || "file"),
      size: Number(r.size) || 0,
      kind: String(r.kind || "document"),
    }));
}
