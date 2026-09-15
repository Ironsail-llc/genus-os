import { describe, it, expect } from "vitest";
import {
  ALL_VIEW_IDS,
  isAuditReaderRole,
  isOperatorRole,
  navGroups,
  settingsPages,
  viewGroupLabel,
  viewTitles,
  visibleNavGroups,
  isComingSoonView,
} from "../nav-config";

describe("navGroups", () => {
  it("is ordered Chat, Workspace, Observe, Settings", () => {
    expect(navGroups.map((g) => g.label)).toEqual([
      "Chat",
      "Workspace",
      "Observe",
      "Settings",
    ]);
  });

  it("puts Inbox, Agents, Automations, Dashboard and Marketplace in Workspace", () => {
    const workspace = navGroups.find((g) => g.label === "Workspace")!;
    const labels = workspace.items.map((i) => i.label);
    for (const label of ["Inbox", "Agents", "Automations", "Dashboard", "Marketplace"]) {
      expect(labels).toContain(label);
    }
  });

  it("keeps the existing Tasks view reachable", () => {
    const every = navGroups.flatMap((g) => g.items);
    expect(every.some((i) => i.view === "tasks")).toBe(true);
  });

  it("puts Runs, Fleet, Memory, Audit, Logs and Health in Observe", () => {
    const observe = navGroups.find((g) => g.label === "Observe")!;
    expect(observe.items.map((i) => i.label)).toEqual([
      "Runs",
      "Fleet",
      "Memory",
      "Audit",
      "Logs",
      "Health",
    ]);
  });

  it("lists the settings pages under the Settings group", () => {
    const settings = navGroups.find((g) => g.label === "Settings")!;
    expect(settings.items.map((i) => i.label)).toEqual([
      "Providers",
      "Channels",
      "Users & roles",
      "Secrets",
      "Config",
      "Flags",
      "Plugins",
      "Appearance",
    ]);
    expect(settings.items.every((i) => i.view === "settings")).toBe(true);
  });

  it("has no placeholder left in it — every nav item reaches a real screen", () => {
    const soon = navGroups
      .flatMap((g) => g.items)
      .filter((i) => i.soon)
      .map((i) => i.view);
    expect(soon).toEqual([]);
    for (const view of ALL_VIEW_IDS) expect(isComingSoonView(view)).toBe(false);
  });

  it("no longer marks Memory, Audit and Logs as coming soon — all three are built", () => {
    const byView = new Map(navGroups.flatMap((g) => g.items).map((i) => [i.view, i]));
    for (const view of ["memory", "audit", "logs"] as const) {
      expect(byView.get(view)!.soon).toBeFalsy();
      expect(isComingSoonView(view)).toBe(false);
    }
  });

  it("no longer marks Inbox as coming soon — it is a real screen now", () => {
    const inbox = navGroups.flatMap((g) => g.items).find((i) => i.view === "inbox")!;
    expect(inbox.soon).toBeFalsy();
    expect(isComingSoonView("inbox")).toBe(false);
  });

  it("gives every nav item a unique id", () => {
    const ids = navGroups.flatMap((g) => g.items).map((i) => i.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("titles every view it can reach", () => {
    for (const item of navGroups.flatMap((g) => g.items)) {
      expect(viewTitles[item.view]).toBeTruthy();
    }
  });

  it("names the group a view belongs to", () => {
    expect(viewGroupLabel("runs")).toBe("Observe");
    expect(viewGroupLabel("chat")).toBe("Chat");
  });
});

describe("role gating", () => {
  it("treats owner and admin as operators", () => {
    expect(isOperatorRole("owner")).toBe(true);
    expect(isOperatorRole("admin")).toBe(true);
    expect(isOperatorRole("viewer")).toBe(false);
    expect(isOperatorRole(undefined)).toBe(false);
  });

  it("hides the Settings group from non-operators", () => {
    expect(visibleNavGroups("viewer").map((g) => g.label)).not.toContain("Settings");
    expect(visibleNavGroups(undefined).map((g) => g.label)).not.toContain("Settings");
    expect(visibleNavGroups("owner").map((g) => g.label)).toContain("Settings");
  });

  /**
   * Mirrors `crm/bridge/routers/_operator.py::AUDIT_ROLES`. The auditor exists
   * so that "who did what, and who widened which guardrail" can be read by
   * somebody who may not change anything and may not read the journal — the
   * journal carries every value every process printed, which is a far wider
   * surface than a record of decisions.
   */
  it("treats the auditor as an audit reader, and an operator as one too", () => {
    expect(isAuditReaderRole("auditor")).toBe(true);
    expect(isAuditReaderRole("owner")).toBe(true);
    expect(isAuditReaderRole("admin")).toBe(true);
    expect(isAuditReaderRole("member")).toBe(false);
    expect(isAuditReaderRole(undefined)).toBe(false);
    // An auditor is NOT an operator: Memory and Logs stay shut to them.
    expect(isOperatorRole("auditor")).toBe(false);
  });

  function observeItems(role: string | undefined): string[] {
    const observe = visibleNavGroups(role).find((g) => g.label === "Observe");
    return observe ? observe.items.map((i) => i.id) : [];
  }

  it("shows an operator all three new Observe screens", () => {
    for (const role of ["owner", "admin"]) {
      expect(observeItems(role)).toEqual(["runs", "fleet", "memory", "audit", "logs", "health"]);
    }
  });

  it("shows an auditor Audit, and neither Memory nor Logs", () => {
    const items = observeItems("auditor");
    expect(items).toContain("audit");
    expect(items).not.toContain("memory");
    expect(items).not.toContain("logs");
    // The screens an auditor shared with everyone before are still there.
    expect(items).toContain("runs");
  });

  it("shows a member none of the three", () => {
    for (const role of ["member", "viewer", undefined]) {
      const items = observeItems(role);
      expect(items).not.toContain("memory");
      expect(items).not.toContain("audit");
      expect(items).not.toContain("logs");
    }
  });

  it("drops a group whose every item the role may not see", () => {
    // Settings is the group with a blanket gate; the per-item gate must not
    // leave an empty heading behind on any other one either.
    for (const role of ["member", "auditor", "owner", undefined]) {
      for (const group of visibleNavGroups(role)) {
        expect(group.items.length).toBeGreaterThan(0);
      }
    }
  });
});

describe("settingsPages", () => {
  it("describes what will live on every page", () => {
    expect(settingsPages.length).toBe(8);
    for (const page of settingsPages) {
      expect(page.label).toBeTruthy();
      expect(page.description.length).toBeGreaterThan(10);
    }
  });
});
