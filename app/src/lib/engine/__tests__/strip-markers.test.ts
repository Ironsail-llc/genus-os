/**
 * Markers are metadata, not text — wherever they sit in a message.
 *
 * Live, 2026-09-14: a reply from main ended in
 * `[DASHBOARD:{"intent":"memory-health","data":{"current":[{"block":"a","age":"2m"}],"stale":[...]}}]`
 * and the Helm rendered everything after the first `}]` — the regex stripper
 * stopped at the first close it saw, which was the end of the `current`
 * array, not the end of the marker. The streaming interceptor already walks
 * the JSON with a depth count; history and final text must use the same walk.
 */
import { describe, expect, it } from "vitest";

import { stripMarkers } from "@/lib/engine/marker-interceptor";

const LIVE =
  '[DASHBOARD:{"intent":"memory-health","data":{"current":[{"block":"working_context","age":"2m"}],' +
  '"stale":[{"block":"persona","age":"~215d","issue":"claims Opus / Daniel voice"}],' +
  '"retrieval":[{"signal":"phone number","state":"correct"}]}}]';

describe("stripMarkers", () => {
  it("removes a trailing dashboard marker whose JSON contains nested arrays", () => {
    const text = `Want me to refresh persona now?\n\n${LIVE}`;
    expect(stripMarkers(text)).toBe("Want me to refresh persona now?");
  });

  it("removes a leading marker and a marker in the middle", () => {
    expect(stripMarkers(`${LIVE}\nHere is the picture.`)).toBe("Here is the picture.");
    expect(stripMarkers(`before ${LIVE} after`)).toBe("before after");
  });

  it("removes render markers with nested props", () => {
    const text = 'Done.\n[RENDER:metric_grid:{"items":[{"t":"Healthy","v":3}]}]\nAnything else?';
    expect(stripMarkers(text)).toBe("Done.\nAnything else?");
  });

  it("leaves ordinary brackets alone", () => {
    const text = "See [the docs](https://example.com) and [engine] context; array[0] = {a: 1}.";
    expect(stripMarkers(text)).toBe(text);
  });

  it("drops an unterminated marker rather than showing half of it", () => {
    expect(stripMarkers('All good.\n[DASHBOARD:{"intent":"x","data":{"a":[1,2')).toBe("All good.");
  });

  it("still removes the un-bracketed variants and the plan marker", () => {
    expect(stripMarkers('ok DASHBOARD:{"intent":"x"}] done')).toBe("ok done");
    expect(stripMarkers("ready [PLAN_READY]")).toBe("ready");
  });
});
