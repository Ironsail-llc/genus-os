/**
 * Shared gateway session state — tracks whether visual canvas prompt
 * has been injected for the webchat session.
 */
import { getEngineClient } from "./server-client";
import { getVisualCanvasPrompt } from "@/lib/system-prompt";
import { SESSION_KEY } from "@/lib/config";
let injected = false;

/** Ensure the visual canvas prompt is injected into the session. No-op after first success. */
export async function ensureCanvasPromptInjected(): Promise<void> {
  if (injected) return;
  const client = getEngineClient();
  try {
    await client.chatInject(
      SESSION_KEY,
      getVisualCanvasPrompt(),
      "visual-canvas-init"
    );
    injected = true;
  } catch {
    // Non-critical — will retry on next call
  }
}

export { SESSION_KEY };
