/**
 * The one way a model-generated dashboard becomes servable HTML.
 *
 * The canvas is read-only, so the sanitizer removes interactive tags (links,
 * forms, buttons, inputs) and keeps their text. Sanitizing FIRST and then
 * validating what will actually be served means a view that merely contains
 * such a tag — every news story carries a link — is served without it,
 * instead of being rejected whole and leaving the canvas unchanged.
 */
import DOMPurify from "isomorphic-dompurify";
import { validateDashboardCode } from "@/lib/dashboard/code-validator";

export const SANITIZE_CONFIG = {
  ADD_TAGS: ["svg", "polyline", "path", "circle", "rect", "line", "text", "g", "defs", "linearGradient", "stop"],
  ADD_ATTR: ["data-testid", "viewBox", "points", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "fill", "d", "cx", "cy", "r", "x1", "y1", "x2", "y2", "offset", "stop-color", "stop-opacity", "height", "width"],
  ALLOW_DATA_ATTR: false,
  ALLOW_UNKNOWN_PROTOCOLS: false,
  FORBID_TAGS: [
    "script",
    "iframe",
    "object",
    "embed",
    "link",
    "meta",
    "base",
    "a",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "option",
    "fieldset",
  ],
  FORBID_ATTR: ["srcdoc"],
};

export type FinalizedDashboard =
  | { ok: true; html: string }
  | { ok: false; errors: string[] };

export function finalizeDashboardHtml(raw: string): FinalizedDashboard {
  if (!raw || !raw.trim()) return { ok: false, errors: ["Empty code"] };
  // First pass only normalises (strips markdown fences); its verdict is
  // about the unsanitized draft and is deliberately not used.
  const draft = validateDashboardCode(raw).code;
  const sanitized = DOMPurify.sanitize(draft, SANITIZE_CONFIG);
  const validation = validateDashboardCode(sanitized);
  if (!validation.valid) return { ok: false, errors: validation.errors };
  return { ok: true, html: validation.code };
}

/**
 * Generate and finalize a dashboard, with one corrective retry: a rejected
 * draft is regenerated with the rejection reasons appended, so a single bad
 * draw does not leave the canvas silently unchanged.
 */
export async function renderDashboard(
  complete: (userPrompt: string) => Promise<string>,
  userPrompt: string,
  attempts = 2,
): Promise<FinalizedDashboard> {
  let prompt = userPrompt;
  let result: FinalizedDashboard = { ok: false, errors: ["not attempted"] };
  for (let attempt = 0; attempt < attempts; attempt++) {
    result = finalizeDashboardHtml(await complete(prompt));
    if (result.ok) return result;
    prompt =
      `${userPrompt}\n\nYour previous output was rejected for: ${result.errors.join("; ")}. ` +
      "Output only static HTML and CSS within the rules above.";
  }
  return result;
}
