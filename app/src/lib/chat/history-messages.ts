import { stripMarkers } from "@/lib/engine/marker-interceptor";

/**
 * Chat history rows, as the engine answers them, turned into messages for the
 * panel.
 *
 * The time is the one the engine stored, when it sends one. When it does not
 * (pages off, or a row only in memory) the message has none, and the bubble
 * shows none: stamping it with the time it loaded is how every message in a
 * week-old conversation used to read "just now".
 */
export interface HistoryMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp?: Date;
  /** The stored row's id: the cursor "Load earlier" pages back from. */
  serverId?: number;
}

type Block = { type: string; text?: string };
type Row = {
  role?: unknown;
  content?: string | Block[];
  id?: unknown;
  timestamp?: unknown;
};

function text(content: Row["content"]): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((b) => b.type === "text")
    .map((b) => b.text || "")
    .join("");
}

function when(value: unknown): Date | undefined {
  if (typeof value !== "string") return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

export function historyToMessages(rows: unknown): HistoryMessage[] {
  if (!Array.isArray(rows)) return [];
  return (rows as Row[])
    .map((row, i) => ({ row, i }))
    .filter(({ row }) => row.role === "user" || row.role === "assistant")
    .map(({ row, i }) => {
      const serverId = typeof row.id === "number" ? row.id : undefined;
      return {
        id: serverId !== undefined ? `hist-${serverId}` : `hist-${i}`,
        role: row.role as "user" | "assistant",
        content: stripMarkers(text(row.content)).trim(),
        timestamp: when(row.timestamp),
        serverId,
      };
    });
}

/** The smallest stored id among the messages: where the next older page ends. */
export function oldestServerId(messages: ReadonlyArray<{ serverId?: number }>): number | undefined {
  let oldest: number | undefined;
  for (const m of messages) {
    if (m.serverId !== undefined && (oldest === undefined || m.serverId < oldest)) oldest = m.serverId;
  }
  return oldest;
}
