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

/** The size limit as people read it: "5 MB". */
export const MAX_ATTACHMENT_SIZE_TEXT = `${MAX_ATTACHMENT_BYTES / (1024 * 1024)} MB`;

/** What the composer says, word for word, about what it takes. */
export const ATTACHMENT_LIMITS_TEXT =
  `Images (PNG, JPG, GIF, WebP) and text, Markdown, CSV, JSON or PDF · up to ${MAX_ATTACHMENT_SIZE_TEXT} each · ${MAX_ATTACHMENTS} per message`;

/**
 * The largest `/api/chat/send` body the route reads: every file at the limit,
 * base64 (4 bytes per 3), plus a megabyte for the message and the JSON.
 */
export const MAX_SEND_BODY_BYTES = Math.ceil((MAX_ATTACHMENT_BYTES * MAX_ATTACHMENTS * 4) / 3) + 1024 * 1024;

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

/** "image", "pdf" or "text": what a type, or an extension, says the file is. */
function familyOf(mime: string): string {
  return mime.startsWith("image/") ? "image" : mime === "application/pdf" ? "pdf" : "text";
}

/** ".png" for "shot.PNG"; "" for no extension, as Python's `PurePath.suffix` says (".env" has none). */
export function extensionOf(name: string): string {
  const base = name.slice(Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\")) + 1);
  const dot = base.lastIndexOf(".");
  return dot <= 0 || dot === base.length - 1 ? "" : base.slice(dot).toLowerCase();
}

/** The MIME type an accepted extension names, or null. */
function mimeOfExtension(ext: string): string | null {
  for (const [mime, exts] of Object.entries(ACCEPTED_TYPES)) {
    if (exts.includes(ext)) return mime;
  }
  return null;
}

/**
 * Do this name and this declared type describe the same kind of file? An
 * extension, when there is one, must be on the list and name the same family
 * as the type: `tool.exe` sent as text, or `evil.png` sent as text, is not.
 * A name with no extension is the type's word alone here; the engine also
 * checks the bytes for those.
 */
export function nameFitsType(name: string, mime: string): boolean {
  const ext = extensionOf(name);
  if (!ext) return true;
  const named = mimeOfExtension(ext);
  return named !== null && familyOf(named) === familyOf(mime);
}

/**
 * The MIME type to send for a file, or null when the turn cannot carry it.
 * A browser often reports no type for `.md` (or a variant like
 * `text/x-markdown`), so the extension decides when the type is missing or
 * not on the list; when both are present they must agree (`nameFitsType`).
 */
export function attachmentMime(name: string, type: string): string | null {
  const lowered = (type || "").toLowerCase();
  if (lowered in ACCEPTED_TYPES) return nameFitsType(name, lowered) ? lowered : null;
  const ext = extensionOf(name);
  return ext ? mimeOfExtension(ext) : null;
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
  if (typeof mime !== "string" || !(mime.toLowerCase() in ACCEPTED_TYPES) || !nameFitsType(name, mime.toLowerCase())) {
    return { status: 422, error: `${name} is not a supported type` };
  }
  if (typeof data !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(data) || data.length % 4 !== 0) {
    return { status: 422, error: `${name} is not valid base64` };
  }
  const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
  const decoded = (data.length / 4) * 3 - padding;
  if (decoded > MAX_ATTACHMENT_BYTES) return { status: 413, error: `${name} is larger than ${MAX_ATTACHMENT_SIZE_TEXT}` };
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
  // The note starts the turn or follows a blank line, and it is the LAST such
  // block: anything earlier that looks like one is the operator's own text.
  const pattern = /(^|\n\n)\[\d+ files? attached — kept on disk; use the paths below\]/g;
  let at = -1;
  for (const match of content.matchAll(pattern)) at = match.index;
  if (at < 0) return content;
  // Only the note block goes: what the operator added while the agent worked
  // (`[added while working] …`, appended after it) is theirs and stays.
  const added = content.indexOf("\n\n[added while working] ", at);
  const kept = content.slice(0, at).trim();
  if (added < 0) return kept;
  const rest = content.slice(added).trim();
  return kept ? `${kept}\n\n${rest}` : rest;
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
