import { describe, expect, it } from "vitest";

import {
  companyStatus,
  failureReasonLabel,
  formatCost,
  formatRate,
  sortCompanies,
  statusLabel,
  type CompanyEntry,
} from "../command-centre-format";

function entry(tenant_id: string, status: string, extra: Partial<CompanyEntry> = {}): CompanyEntry {
  return { tenant_id, display_name: tenant_id, parent_id: null, status, ...extra } as CompanyEntry;
}

describe("statusLabel", () => {
  it("names every status in words, so colour is never the only signal", () => {
    expect(statusLabel("ok")).toBe("OK");
    expect(statusLabel("attention")).toBe("Needs attention");
    expect(statusLabel("no_activity")).toBe("No activity");
    expect(statusLabel("error")).toBe("Could not read");
  });

  it("shows an unknown status as itself rather than as healthy", () => {
    expect(statusLabel("something_new")).toBe("something_new");
  });
});

describe("companyStatus", () => {
  it("treats an entry that carries an error as an error whatever its status says", () => {
    expect(companyStatus(entry("acme-child-b", "ok", { error: "could not read this tenant" }))).toBe(
      "error",
    );
  });

  it("keeps the rollup's status otherwise", () => {
    expect(companyStatus(entry("acme-parent", "attention"))).toBe("attention");
  });
});

describe("sortCompanies", () => {
  it("puts companies that need attention first, then unreadable ones, then quiet, then ok", () => {
    const sorted = sortCompanies([
      entry("acme-parent", "ok"),
      entry("acme-quiet", "no_activity"),
      entry("acme-broken", "error", { error: "could not read this tenant" }),
      entry("acme-child-a", "attention"),
    ]);
    expect(sorted.map((e) => e.tenant_id)).toEqual([
      "acme-child-a",
      "acme-broken",
      "acme-quiet",
      "acme-parent",
    ]);
  });

  it("keeps the order given among companies with the same status", () => {
    const sorted = sortCompanies([
      entry("acme-parent", "ok"),
      entry("acme-child-a", "ok"),
      entry("acme-child-b", "ok"),
    ]);
    expect(sorted.map((e) => e.tenant_id)).toEqual(["acme-parent", "acme-child-a", "acme-child-b"]);
  });

  it("does not reorder the list it was given", () => {
    const given = [entry("acme-parent", "ok"), entry("acme-child-a", "attention")];
    sortCompanies(given);
    expect(given.map((e) => e.tenant_id)).toEqual(["acme-parent", "acme-child-a"]);
  });
});

describe("formatting", () => {
  it("shows a success rate as a whole percentage, and none as a dash", () => {
    expect(formatRate(0.8571)).toBe("86%");
    expect(formatRate(1)).toBe("100%");
    expect(formatRate(null)).toBe("—");
    expect(formatRate(undefined)).toBe("—");
  });

  it("shows cost in dollars to the cent", () => {
    expect(formatCost(3.25)).toBe("$3.25");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(undefined)).toBe("$0.00");
  });

  it("names the classified failure reason in words", () => {
    expect(failureReasonLabel("timeout")).toBe("timeout");
    expect(failureReasonLabel("rate_limit")).toBe("rate limit");
    expect(failureReasonLabel("budget_exhausted")).toBe("budget exhausted");
    expect(failureReasonLabel(undefined)).toBe("unknown");
  });
});
