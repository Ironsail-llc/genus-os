/**
 * The home quick actions' labels, kept apart from their handlers and icons so
 * the chat panel can compare against them without importing either.
 */
export const QUICK_ACTION_LABELS = {
  contacts: "Show my contacts",
  inbox: "Check inbox",
  memory: "Search memory",
  health: "Service health",
} as const;

/** Lower-case, no punctuation, no "the": "Check the inbox" reads as "check inbox". */
function normaliseLabel(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, "")
    .split(/\s+/)
    .filter((word) => word && word !== "the")
    .join(" ");
}

const NORMALISED = new Set(Object.values(QUICK_ACTION_LABELS).map(normaliseLabel));

/** True when a suggested prompt says the same thing as a quick action. */
export function repeatsQuickAction(prompt: string): boolean {
  return NORMALISED.has(normaliseLabel(prompt));
}
