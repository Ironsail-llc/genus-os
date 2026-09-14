/**
 * The one reader of a refused bridge reply in the Helm.
 *
 * There used to be three byte-near copies of this — in `lib/agents/manifests.ts`,
 * in `views/settings/providers-page.tsx` and in `lib/inbox/pending.ts` — and
 * only the newest of them knew about `message`. That is not a tidiness point:
 * the bridge answers refusals in three different shapes depending on which
 * layer refused, and a reader that knows two of them prints
 * `HTTP 400` over a sentence the operator needed.
 *
 * The three shapes, in the order they are tried:
 *
 * `{message}`
 *     What a router writes itself. `_refuse` in
 *     `crm/bridge/routers/approvals.py` answers `{"settled": false, "message": …}`;
 *     `controls.py` and `fleet.py` do the same. Tried FIRST, because a body
 *     carrying both is a router deliberately overriding the framework.
 * `{detail}`
 *     FastAPI's own — an `HTTPException`'s string, or the list of
 *     `{loc, msg, type}` objects a 422 validation failure produces.
 * `{error}`
 *     What the app's own BFF proxy (`app/api/bridge/[...path]`) writes when it
 *     cannot reach the bridge at all.
 *
 * Nothing here ever throws. A non-JSON body, an empty body, a `message` of
 * three spaces — every one of them falls through to the status line rather
 * than losing the only thing the caller could have told the operator.
 */

/** The server's own words, whichever field it used; the status line if it gave none. */
export async function readBridgeReply(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object") {
      const message = (body as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message;

      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
      if (Array.isArray(detail)) {
        const messages = detail
          .map((item) => (item && typeof item === "object" ? (item as { msg?: string }).msg : null))
          .filter((msg): msg is string => Boolean(msg));
        if (messages.length) return messages.join("; ");
      }

      const error = (body as { error?: unknown }).error;
      if (typeof error === "string" && error.trim()) return error;
    }
  } catch {
    // A non-JSON body is not a reason to lose the status code below.
  }
  return `The bridge refused the request (HTTP ${res.status}).`;
}
