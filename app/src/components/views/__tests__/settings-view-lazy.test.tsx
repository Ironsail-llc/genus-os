import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

/**
 * Settings downloads only the sub-page being shown. Each page module is a
 * stand-in whose factory records the import, so the record is exactly the set
 * of page modules Settings asked for.
 */
const lazy = vi.hoisted(() => ({ loaded: [] as string[] }));
const reload = vi.hoisted(() => ({ page: vi.fn() }));
vi.mock("@/lib/reload-page", () => ({ reloadPage: () => reload.page() }));

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

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SettingsView — sub-pages load on demand", () => {
  beforeEach(() => {
    vi.resetModules();
    lazy.loaded = [];
    reload.page.mockClear();
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

  it("says a page that failed to download needs a reload, not another page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.doMock("@/components/views/settings/plugins-page", () => {
      const error = new Error("Failed to load chunk /_next/static/chunks/plugins.js");
      error.name = "ChunkLoadError";
      throw error;
    });
    await renderSettings("plugins");

    const panel = await screen.findByTestId("settings-page-load-failed");
    expect(panel.textContent).toMatch(/updated/i);
    expect(panel.textContent).not.toMatch(/answer from the engine/i);
    expect(screen.queryByTestId("settings-page-error")).toBeNull();
    fireEvent.click(screen.getByTestId("settings-page-load-failed-reload"));
    expect(reload.page).toHaveBeenCalledTimes(1);
  });
});
