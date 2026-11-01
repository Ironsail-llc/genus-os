import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

/**
 * Settings downloads only the sub-page being shown. Each page module is a
 * stand-in whose factory records the import, so the record is exactly the set
 * of page modules Settings asked for.
 */
const lazy = vi.hoisted(() => ({ loaded: [] as string[], failing: null as string | null }));
const reload = vi.hoisted(() => ({ page: vi.fn() }));
vi.mock("@/lib/reload-page", () => ({ reloadPage: () => reload.page() }));

function stub(name: string) {
  lazy.loaded.push(name);
  return function StubPage() {
    return <div data-testid={`stub-${name}-page`} />;
  };
}

/**
 * A page module's stand-in, or, for the page a test names in `lazy.failing`,
 * one whose chunk did not download. The export throws on access, as the
 * dynamic import's `.then` reads it: the loader rejects with the chunk error
 * itself. (An error thrown by the factory reaches it wrapped in vitest's own
 * message.)
 *
 * The choice is made here, when the module is imported, and not by a second
 * `vi.doMock` of the same path in the test: vitest resolves the mocks queued
 * before an import in parallel and registers each as its path resolves, so of
 * two queued for one path either can win, and the page sometimes loaded fine.
 */
function pageModule(exportName: string, name: string) {
  if (lazy.failing === name) {
    const error = new Error(`Failed to load chunk /_next/static/chunks/${name}.js`);
    error.name = "ChunkLoadError";
    return {
      get [exportName]() {
        throw error;
      },
    };
  }
  return { [exportName]: stub(name) };
}

/** Fresh factories per test (see app-shell-lazy.test.tsx for why `doMock`). */
const PAGE_MODULES: Array<[path: string, exportName: string, name: string]> = [
  ["@/components/views/settings/channels-page", "ChannelsPage", "channels"],
  ["@/components/views/settings/config-page", "ConfigPage", "config"],
  ["@/components/views/settings/flags-page", "FlagsPage", "flags"],
  ["@/components/views/settings/plugins-page", "PluginsPage", "plugins"],
  ["@/components/views/settings/providers-page", "ProvidersPage", "providers"],
  ["@/components/views/settings/users-page", "UsersPage", "users"],
  ["@/components/views/settings/secrets-page", "SecretsPage", "secrets"],
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

// A slow machine can take longer than the default second to import a page,
// or to reject the import and reach the error boundary.
const CHUNK_WAIT = { timeout: 5000 };

describe("SettingsView — sub-pages load on demand", () => {
  beforeEach(() => {
    vi.resetModules();
    lazy.loaded = [];
    lazy.failing = null;
    reload.page.mockClear();
    for (const [path, exportName, name] of PAGE_MODULES) {
      vi.doMock(path, () => pageModule(exportName, name));
    }
  });

  it("imports only the plugins page module when Plugins is open", async () => {
    await renderSettings("plugins");
    expect(await screen.findByTestId("stub-plugins-page", {}, CHUNK_WAIT)).toBeInTheDocument();
    expect(lazy.loaded).toEqual(["plugins"]);
  });

  it("imports no page module for a page without one", async () => {
    await renderSettings("appearance");
    expect(await screen.findByTestId("settings-page-appearance", {}, CHUNK_WAIT)).toBeInTheDocument();
    expect(lazy.loaded).toEqual([]);
  });

  it("says a page that failed to download needs a reload, not another page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    lazy.failing = "plugins";
    await renderSettings("plugins");

    const panel = await screen.findByTestId("settings-page-load-failed", {}, CHUNK_WAIT);
    expect(panel.textContent).toMatch(/updated/i);
    expect(panel.textContent).not.toMatch(/answer from the engine/i);
    expect(screen.queryByTestId("settings-page-error")).toBeNull();
    fireEvent.click(screen.getByTestId("settings-page-load-failed-reload"));
    expect(reload.page).toHaveBeenCalledTimes(1);
  });
});
