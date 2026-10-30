/**
 * Which hop answered a 502, in words.
 *
 * A leaf module: the Plugins page and its Install card both use these, and
 * keeping them in the page made the card import the page that imports it.
 */

import { readBridgeReply } from "@/lib/bridge/read-reply";

/** What a 502 cost on the Plugins page's reads and writes; Install says its own. */
const NOT_READ = "the plugin set could not be read";

/**
 * The sentence for a 502, by which hop answered it.
 *
 * Two hops can say 502 here and they need different remedies. The bridge
 * writes `{"error": "engine unavailable"}` when IT cannot reach the engine;
 * the app's own proxy writes `{"error": "Bridge service unavailable"}` when the
 * dashboard cannot reach the bridge at all. Calling the second one "the engine
 * is unreachable" sends an operator to check a service that may be fine.
 * `reply` is what `readBridgeReply` read from the body; anything else keeps the
 * generic sentence, because a 502 neither hop claimed proves nothing about
 * which one failed.
 */
export function unreachableSentence(reply: string, outcome: string = NOT_READ): string {
  if (reply === "engine unavailable") {
    return (
      `The bridge could not reach the engine at its configured URL (ROBOTHOR_ENGINE_URL), so ${outcome}. ` +
      "Check that the engine is running and that the network path from the bridge to the engine " +
      "is allowed. Nothing has been changed."
    );
  }
  if (reply === "Bridge service unavailable") {
    return (
      `The dashboard could not reach the bridge, so ${outcome}. ` +
      "Check that the bridge service is running. Nothing has been changed."
    );
  }
  return `The engine is unreachable from the bridge, so ${outcome}. Nothing has been changed.`;
}

/** A refused reply in words: the hop's sentence for a 502, the server's own otherwise. */
export async function refusalSentence(res: Response, outcome: string = NOT_READ): Promise<string> {
  const reply = await readBridgeReply(res);
  return res.status === 502 ? unreachableSentence(reply, outcome) : reply;
}
