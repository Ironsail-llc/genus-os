import { describe, expect, it } from "vitest";

import {
  companyStatus,
  connectionsByTenant,
  countRemoteTools,
  failureReasonLabel,
  formatCost,
  formatRate,
  sortCompanies,
  statusLabel,
  summariseChecks,
  type CompanyEntry,
  type CompanyConnections,
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

describe("summariseChecks", () => {
  it("counts checks by status and names the failing and warning ones", () => {
    expect(
      summariseChecks([
        { name: "example-plugin.connection", status: "ok", detail: "" },
        { name: "example-plugin.token", status: "fail", detail: "expired" },
        { name: "other-plugin.connection", status: "warn", detail: "slow" },
        { name: "other-plugin.webhook", status: "fail", detail: "" },
        { name: "other-plugin.optional", status: "skip", detail: "" },
      ]),
    ).toEqual({
      ok: 1,
      warn: 1,
      fail: 2,
      skip: 1,
      failing: ["example-plugin.token", "other-plugin.webhook"],
      warning: ["other-plugin.connection"],
    });
  });

  it("counts an unknown check status as a warning, never as ok", () => {
    const summary = summariseChecks([{ name: "x.check", status: "weird", detail: "" }]);
    expect(summary.ok).toBe(0);
    expect(summary.warn).toBe(1);
    expect(summary.warning).toEqual(["x.check"]);
  });

  it("is all zeros for no checks", () => {
    expect(summariseChecks([])).toEqual({ ok: 0, warn: 0, fail: 0, skip: 0, failing: [], warning: [] });
  });
});

describe("countRemoteTools", () => {
  it("splits the remote tools into reads and writes", () => {
    expect(
      countRemoteTools([
        { name: "a", plugin: "p", access: "read", attestations: [], description: "" },
        { name: "b", plugin: "p", access: "read", attestations: [], description: "" },
        { name: "c", plugin: "p", access: "write", attestations: ["baa"], description: "" },
      ]),
    ).toEqual({ read: 2, write: 1 });
  });

  it("is zero for none", () => {
    expect(countRemoteTools([])).toEqual({ read: 0, write: 0 });
  });
});

describe("connectionsByTenant", () => {
  const entryFor = (tenant_id: string): CompanyConnections => ({
    tenant_id,
    display_name: tenant_id,
    status: "ok",
    error: null,
    plugins: [],
    tools: [],
    checks: [],
  });

  it("indexes the entries by tenant id", () => {
    const map = connectionsByTenant({
      as_of: "",
      home_tenant_id: "acme-parent",
      tenants: [entryFor("acme-child-a"), entryFor("acme-child-b")],
    });
    expect(map.get("acme-child-a")?.tenant_id).toBe("acme-child-a");
    expect(map.get("acme-parent")).toBeUndefined();
  });

  it("is empty when there is no answer", () => {
    expect(connectionsByTenant(null).size).toBe(0);
  });
});
