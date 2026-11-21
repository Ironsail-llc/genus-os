/**
 * In-app navigation from anywhere: a workspace link in a chat reply, an
 * Inbox note, "Open as workspace" on a task.
 *
 * The shell's route lives in the query string (`hooks/use-view-route.ts`) and
 * follows `popstate`. So moving to a route is: push it onto history, then
 * announce it the way the browser announces Back. Every `useViewRoute` reader
 * picks it up; nothing reloads, and a component needs no callback threaded
 * down from the shell to do it.
 */

/**
 * The in-app route an href names (`/?v=…`), or null when it is anything else:
 * another site, another path, an anchor, a mailto. Relative, or absolute on
 * this origin.
 */
export function appRouteOf(href: string | null | undefined): string | null {
  if (typeof href !== "string" || !href || href.startsWith("#")) return null;
  if (typeof window === "undefined") return null;
  let url: URL;
  try {
    url = new URL(href, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin || url.pathname !== "/") return null;
  if (!url.searchParams.has("v")) return null;
  return `/${url.search}`;
}

/** Go to an in-app route without a page load. */
export function navigateTo(route: string): void {
  if (typeof window === "undefined") return;
  window.history.pushState(null, "", route);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** A plain left click: anything else (new tab, new window, download) is the browser's. */
export function isPlainClick(event: {
  button: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
  defaultPrevented: boolean;
}): boolean {
  return (
    event.button === 0 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey &&
    !event.defaultPrevented
  );
}
