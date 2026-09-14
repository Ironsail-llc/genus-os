import { describe, it, expect } from "vitest";
import {
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

  it("marks views that do not exist yet as coming soon", () => {
    const soon = navGroups
      .flatMap((g) => g.items)
      .filter((i) => i.soon)
      .map((i) => i.view);
    expect(soon.sort()).toEqual(["audit", "inbox", "logs", "memory"]);
    for (const view of soon) expect(isComingSoonView(view)).toBe(true);
    expect(isComingSoonView("agents")).toBe(false);
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
