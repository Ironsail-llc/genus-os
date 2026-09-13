/**
 * Shared gateway session state — tracks whether the visual canvas prompt has
 * been injected for the current user's webchat session.
 *
 * The frontend sends NO session key: the engine derives one per authenticated
 * user (`robothor/engine/chat.py::_effective_session_key`, per-user by
 * default), so there is nothing useful the app could say about which session a
 * request belongs to and a key it did send would be a browser naming whose
 * conversation to join.
 *
 * The dedup bucket is therefore purely Next-side bookkeeping. A single
 * process-global "injected" boolean assumed one shared engine session — with
 * per-user fan-out, only whichever user hit this code path first would ever get
 * the canvas prompt, since everyone else's derived session never received it.
 * Keyed by the Auth.js user id instead (mirrors bridge-auth.ts's use of
 * `auth()`), falling back to one shared bucket when no identity is available
 * (no session, or an anonymous/dev-mode request).
 */
import { getEngineClient } from "./server-client";
import { getVisualCanvasPrompt } from "@/lib/system-prompt";
import { auth } from "@/lib/auth";

/** The dedup bucket used when there is no identity to key on. Never sent anywhere. */
const ANONYMOUS_BUCKET = "anonymous";

const injectedFor = new Set<string>();

async function injectionDedupKey(): Promise<string> {
  try {
    const session = await auth();
    if (session?.user?.id) return session.user.id;
  } catch {
    // Session lookup failure — treat the same as "no identity available".
  }
  return ANONYMOUS_BUCKET;
}

/** Ensure the visual canvas prompt is injected into the session. No-op after first success for a given user. */
export async function ensureCanvasPromptInjected(): Promise<void> {
  const key = await injectionDedupKey();
  if (injectedFor.has(key)) return;
  const client = getEngineClient();
  try {
    await client.chatInject(getVisualCanvasPrompt(), "visual-canvas-init");
    injectedFor.add(key);
  } catch (err) {
    console.warn("[session-state] Canvas prompt injection failed:", (err as Error).message);
    // Non-critical — will retry on next call
  }
}
