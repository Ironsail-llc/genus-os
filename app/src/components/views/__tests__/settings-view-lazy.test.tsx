import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

/**
 * Settings downloads only the sub-page being shown. Each page module is a
 * stand-in whose factory records the import, so the record is exactly the set
 * of page modules Settings asked for.
 */
const lazy = vi.hoisted(() => ({ loaded: [] as string[] }));

function stub(name: string) {
  lazy.loaded.push(name);
  return function StubPage() {
    return <div data-testid={`stub-${name}-page`} />;
  };
}

/** Fresh factories per test (see app-shell-lazy.test.tsx for why `doMock`). */
const PAGE_MODULES: Array<[path: string, exportName: string, name: string]> = [
  ["@/components/views/settings/channels-page", "ChannelsPage", "channels"],
  ["@/components/views/settings/config-page", "ConfigPage", "config"],
  ["@/components/views/settings/flags-page", "FlagsPage", "flags"],
  ["@/components/views/settings/plugins-page", "PluginsPage", "plugins"],
  ["@/components/views/settings/providers-page", "ProvidersPage", "providers"],
  ["@/components/views/settings/users-page", "UsersPage", "users"],
];

async function renderSettings(page: string) {
  const { SettingsView } = await import("../settings-view");
  return render(
    <SettingsView
      visible
      page={page as never}
      onPageChange={() => {}}
      role="owner"
      roleLoading={false}
    />,
  );
}

describe("SettingsView — sub-pages load on demand", () => {
  beforeEach(() => {
    vi.resetModules();
    lazy.loaded = [];
    for (const [path, exportName, name] of PAGE_MODULES) {
      vi.doMock(path, () => ({ [exportName]: stub(name) }));
    }
  });

  it("imports only the plugins page module when Plugins is open", async () => {
    await renderSettings("plugins");
    expect(await screen.findByTestId("stub-plugins-page")).toBeInTheDocument();
    expect(lazy.loaded).toEqual(["plugins"]);
  });

  it("imports no page module for a page without one", async () => {
    await renderSettings("appearance");
    expect(await screen.findByTestId("settings-page-appearance")).toBeInTheDocument();
    expect(lazy.loaded).toEqual([]);
  });
});
