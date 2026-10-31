/**
 * The pure half of the workflow run views: the step tally a run row prints,
 * how a recorded step input or output is shown, and the truncation marker the
 * engine writes in place of a payload over its size cap.
 */
import { describe, expect, it } from "vitest";

import {
  prettyJson,
  runUrl,
  runsUrl,
  stepTally,
  truncationOf,
} from "../workflow-runs";

describe("stepTally", () => {
  it("counts the steps that finished and the ones that failed", () => {
    expect(stepTally({ steps_completed: 2, steps_failed: 1, steps_skipped: 0 })).toBe(
      "2 ok, 1 failed"
    );
  });

  it("names skipped steps only when there are some", () => {
    expect(stepTally({ steps_completed: 1, steps_failed: 0, steps_skipped: 2 })).toBe(
      "1 ok, 0 failed, 2 skipped"
    );
  });

  it("says nothing rather than claiming zero when the counts were never recorded", () => {
    expect(stepTally({})).toBeNull();
    expect(stepTally({ steps_completed: null, steps_failed: null })).toBeNull();
  });
});

describe("truncationOf", () => {
  it("reads the engine's truncation marker", () => {
    expect(
      truncationOf({ _truncated: true, original_bytes: 70000, preview: '{"rows": [1, 2' })
    ).toEqual({ originalBytes: 70000, preview: '{"rows": [1, 2' });
  });

  it("is null for an ordinary payload, including one with a falsy marker", () => {
    expect(truncationOf({ query: "status:open" })).toBeNull();
    expect(truncationOf({ _truncated: false, preview: "x" })).toBeNull();
    expect(truncationOf(null)).toBeNull();
    expect(truncationOf("text")).toBeNull();
    expect(truncationOf([1, 2])).toBeNull();
  });
});

describe("prettyJson", () => {
  it("indents an object", () => {
    expect(prettyJson({ a: 1, b: [true] })).toBe('{\n  "a": 1,\n  "b": [\n    true\n  ]\n}');
  });

  it("indents a string that holds JSON, and leaves plain text alone", () => {
    expect(prettyJson('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(prettyJson("refused: read-only")).toBe("refused: read-only");
  });

  it("renders null and undefined as nothing", () => {
    expect(prettyJson(null)).toBe("");
    expect(prettyJson(undefined)).toBe("");
  });
});

describe("bridge urls", () => {
  it("escapes the workflow id and the run id", () => {
    expect(runsUrl("a/b c")).toBe("/api/bridge/api/workflows/a%2Fb%20c/runs");
    expect(runUrl("wf", "r/1")).toBe("/api/bridge/api/workflows/wf/runs/r%2F1");
  });
});
