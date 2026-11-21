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
import { getVisualCanvasPrompt, getWorkspaceRulesPrompt } from "@/lib/system-prompt";
import { roomsServed } from "@/lib/rooms/served";
import { auth } from "@/lib/auth";

/** The dedup bucket used when there is no identity to key on. Never sent anywhere. */
const ANONYMOUS_BUCKET = "anonymous";

/**
 * Per dedup key: whether the canvas prompt is in, whether the workspace rules
 * are, and until when a "not served" answer stands. Only definite answers are
 * kept: "served" for good, "not served" for NOT_SERVED_TTL_MS (so workspaces
 * switched on later reach the session without a restart), and an unknown
 * answer (timeout, error) not at all.
 */
interface Injected {
  canvas: boolean;
  workspaces: boolean;
  notServedUntil: number;
}

/** How long a "workspaces are not served" answer is trusted. */
export const NOT_SERVED_TTL_MS = 10 * 60_000;

const injectedFor = new Map<string, Injected>();

async function injectionDedupKey(): Promise<string> {
  try {
    const session = await auth();
    if (session?.user?.id) return session.user.id;
  } catch {
    // Session lookup failure — treat the same as "no identity available".
  }
  return ANONYMOUS_BUCKET;
}

/**
 * Ensure the visual canvas prompt is injected into the session, once per user,
 * with the workspace rules where workspaces are served. A session injected
 * before workspaces were served gets the rules alone once they are.
 */
export async function ensureCanvasPromptInjected(): Promise<void> {
  const key = await injectionDedupKey();
  const state = injectedFor.get(key) ?? { canvas: false, workspaces: false, notServedUntil: 0 };
  if (state.canvas && (state.workspaces || Date.now() < state.notServedUntil)) return;

  // The workspace rules name room tools: only where the rooms list is served.
  const served = await roomsServed();
  const client = getEngineClient();
  try {
    if (!state.canvas) {
      await client.chatInject(getVisualCanvasPrompt({ workspaces: served === "served" }), "visual-canvas-init");
      state.canvas = true;
    } else if (served === "served") {
      await client.chatInject(getWorkspaceRulesPrompt(), "workspace-rules-init");
    }
    if (served === "served") state.workspaces = true;
    if (served === "not_served") state.notServedUntil = Date.now() + NOT_SERVED_TTL_MS;
    injectedFor.set(key, state);
  } catch (err) {
    console.warn("[session-state] Canvas prompt injection failed:", (err as Error).message);
    // Non-critical — will retry on next call
  }
}
