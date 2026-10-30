/**
 * Whether the docked chat panel is open, remembered per browser.
 *
 * Open is the default: a browser that has never collapsed the dock, or one
 * whose storage refuses to answer, shows the chat. Only an explicit stored
 * collapse closes it.
 */

/** Where the browser remembers the choice. Per browser, never in the URL. */
export const CHAT_DOCK_STORAGE_KEY = "genus.chatDockOpen";

/** Whether the dock should be open. True unless a collapse was stored. */
export function readChatDockOpen(): boolean {
  try {
    if (typeof window === "undefined") return true;
    return window.localStorage.getItem(CHAT_DOCK_STORAGE_KEY) !== "false";
  } catch {
    return true;
  }
}

/** Remember the choice for the next load. */
export function writeChatDockOpen(open: boolean): void {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(CHAT_DOCK_STORAGE_KEY, String(open));
  } catch {
    // A browser that will not remember opens the dock next time. The choice
    // still holds for this page.
  }
}
