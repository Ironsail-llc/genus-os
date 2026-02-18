/**
 * Conversation message types and trivial response detection.
 *
 * The regex-based detectTopic() has been replaced by LLM-based triage
 * in triage-prompt.ts. This module retains:
 * - isTrivialResponse() — fast client-side guard (saves an API call for "ok"/"thanks")
 * - ConversationMessage / MarkerHint types — used by the generate route
 */

export interface ConversationMessage {
  role: "user" | "assistant";
  content: string;
}

export interface MarkerHint {
  intent: string;
  data?: Record<string, unknown>;
}

const TRIVIAL_PATTERNS = [
  /^(ok|okay|sure|yes|no|nah|nope|yep|yea|yeah|done|thanks|thank you|got it|cool|great|nice|alright|perfect|fine|np|ty|thx|k|bye|goodbye|hello|hi|hey|yo|sup|hm+|huh|ah|oh|wow|lol|haha|hmm)[\s!.?]*$/i,
  /^you'?re welcome[\s!.?]*$/i,
  /^no problem[\s!.?]*$/i,
  /^sounds good[\s!.?]*$/i,
  /^will do[\s!.?]*$/i,
  /^not? (much|really|yet)[\s!.?]*$/i,
  /^👍|🙏|✅|🎉|💯$/,
];

const TRIVIAL_MAX_LENGTH = 50;

export function isTrivialResponse(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed) return true;
  if (trimmed.length > TRIVIAL_MAX_LENGTH) return false;
  return TRIVIAL_PATTERNS.some((p) => p.test(trimmed));
}
