import { describe, expect, it } from "vitest";
import { OUTCOME_UNKNOWN, terminalOutcome } from "../terminal-outcome";

describe("interrupted audited delivery", () => {
  it.each(["cancelled", "failed", "timeout"])("displays settled host evidence for %s", (status) => {
    const text = "The task was created. Execution was interrupted. Any remaining work is not confirmed.";
    expect(terminalOutcome({ status, text, audit_outcome: true }, "partial")).toBe(text);
    expect(terminalOutcome({ status, text }, "partial")).toBe(OUTCOME_UNKNOWN);
    expect(terminalOutcome({ status, text, audit_outcome: "true" }, "partial")).toBe(OUTCOME_UNKNOWN);
  });
  it("keeps recovery for an aborted stream without usable host evidence", () => {
    expect(terminalOutcome({ aborted: true, audit_outcome: true, text: "" }, "partial")).toBe(OUTCOME_UNKNOWN);
    expect(terminalOutcome({ aborted: true, audit_outcome: true, text: "Stored task verified." }, "partial")).toBe("Stored task verified.");
  });
});
