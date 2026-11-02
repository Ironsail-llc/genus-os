import { describe, it, expect } from "vitest";

import { refusalSentence, unreachableSentence } from "../hop-sentence";

/**
 * Which hop a 502 came from, in words. Moved here from the Plugins page test
 * with the mapping itself, so the page and the Install card share one leaf
 * module instead of importing each other.
 */
describe("unreachableSentence — which hop a 502 came from", () => {
  it("maps each hop's 502 body to its own sentence, keeping the act's outcome", () => {
    expect(unreachableSentence("engine unavailable", "nothing was installed")).toBe(
      "The bridge could not reach the engine at its configured URL (ROBOTHOR_ENGINE_URL), so " +
        "nothing was installed. Check that the engine is running and that the network path " +
        "from the bridge to the engine is allowed. Nothing has been changed."
    );
    expect(unreachableSentence("Bridge service unavailable", "nothing was installed")).toBe(
      "The dashboard could not reach the bridge, so nothing was installed. Check that the " +
        "bridge service is running. Nothing has been changed."
    );
    expect(unreachableSentence("The bridge refused the request (HTTP 502).")).toBe(
      "The engine is unreachable from the bridge, so the plugin set could not be read. " +
        "Nothing has been changed."
    );
  });
});

describe("refusalSentence", () => {
  it("uses the hop's sentence for a 502 and the server's own words otherwise", async () => {
    const engine502 = new Response(JSON.stringify({ error: "engine unavailable" }), {
      status: 502,
    });
    expect(await refusalSentence(engine502, "nothing was installed")).toMatch(
      /^The bridge could not reach the engine/
    );

    const refused = new Response(JSON.stringify({ detail: "no such plugin" }), { status: 404 });
    expect(await refusalSentence(refused)).toBe("no such plugin");
  });
});
