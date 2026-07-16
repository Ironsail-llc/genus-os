/**
 * Shared gateway session state — tracks whether visual canvas prompt
 * has been injected for the webchat session.
 *
 * The frontend always sends the same literal SESSION_KEY constant; the
 * engine (robothor/engine/chat.py `_effective_session_key`) is what fans
 * that shared key out into one session per dashboard user when
 * ROBOTHOR_PER_USER_SESSIONS=enforce. A single process-global "injected"
 * boolean assumed one shared engine session — with per-user fan-out, only
 * whichever user hit this code path first would ever get the canvas
 * prompt, since everyone else's *derived* session never received it.
 *
 * Fixed by deduping per identity instead of globally: keyed by the Auth.js
 * user id when a session is available (mirrors bridge-auth.ts's use of
 * `auth()`), falling back to the shared SESSION_KEY constant when it isn't
 * (no session, or an anonymous/dev-mode request). In the single-user or
 * flag-off case there is only ever one distinct key, so this is
 * behaviorally identical to the old boolean.
 */
import { getEngineClient } from "./server-client";
import { getVisualCanvasPrompt } from "@/lib/system-prompt";
import { SESSION_KEY } from "@/lib/config";
import { auth } from "@/lib/auth";

const injectedFor = new Set<string>();

async function injectionDedupKey(): Promise<string> {
  try {
    const session = await auth();
    if (session?.user?.id) return session.user.id;
  } catch {
    // Session lookup failure — treat the same as "no identity available".
  }
  return SESSION_KEY;
}

/** Ensure the visual canvas prompt is injected into the session. No-op after first success for a given user. */
export async function ensureCanvasPromptInjected(): Promise<void> {
  const key = await injectionDedupKey();
  if (injectedFor.has(key)) return;
  const client = getEngineClient();
  try {
    await client.chatInject(
      SESSION_KEY,
      getVisualCanvasPrompt(),
      "visual-canvas-init"
    );
    injectedFor.add(key);
  } catch (err) {
    console.warn("[session-state] Canvas prompt injection failed:", (err as Error).message);
    // Non-critical — will retry on next call
  }
}

export { SESSION_KEY };
