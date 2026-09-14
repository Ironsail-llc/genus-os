import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SettingsView } from "../settings-view";

vi.mock("../controls-view", () => ({
  ControlsView: ({ visible }: { visible?: boolean }) =>
    visible ? <div data-testid="controls-view" /> : null,
}));

// The Providers page is real and fetches on mount. A request that never
// settles leaves it in its loading state for the whole of this suite, which is
// what these tests are about — the container, not the page.
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderSettings(overrides: Partial<React.ComponentProps<typeof SettingsView>> = {}) {
  const props: React.ComponentProps<typeof SettingsView> = {
    visible: true,
    page: "providers",
    onPageChange: vi.fn(),
    role: "owner",
    ...overrides,
  };
  return { props, ...render(<SettingsView {...props} />) };
}

describe("SettingsView", () => {
  it("renders a sub-navigation of grouped pages", () => {
    renderSettings();
    const subnav = screen.getByTestId("settings-subnav");
    expect(subnav.tagName).toBe("NAV");
    for (const id of [
      "providers",
      "channels",
      "users",
      "secrets",
      "config",
      "flags",
      "plugins",
      "appearance",
    ]) {
      expect(screen.getByTestId(`settings-nav-${id}`)).toBeInTheDocument();
    }
    // grouped, not a flat tab strip
    expect(subnav.querySelectorAll("[data-testid^='settings-subgroup-']").length).toBeGreaterThan(1);
  });

  it("marks the active page with aria-current and shows its content", () => {
    renderSettings({ page: "secrets" });
    expect(screen.getByTestId("settings-nav-secrets")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("settings-page-secrets")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-page-providers")).toBeNull();
  });

  it("says what will live on a placeholder page", () => {
    renderSettings({ page: "channels" });
    const page = screen.getByTestId("settings-page-channels");
    expect(page.textContent).toMatch(/soon/i);
    expect(page.textContent!.length).toBeGreaterThan(30);
  });

  it("changes page on click", () => {
    const onPageChange = vi.fn();
    renderSettings({ onPageChange });
    fireEvent.click(screen.getByTestId("settings-nav-plugins"));
    expect(onPageChange).toHaveBeenCalledWith("plugins");
  });

  it("gives the Flags page the existing controls screen", () => {
    renderSettings({ page: "flags" });
    expect(screen.getByTestId("controls-view")).toBeInTheDocument();
  });

  it("renders nothing when not visible", () => {
    const { container } = renderSettings({ visible: false });
    expect(container.querySelector("[data-testid='settings-view']")).toBeNull();
  });

  it("tells a non-operator the screen is not theirs", () => {
    renderSettings({ role: "viewer" });
    expect(screen.getByTestId("settings-restricted")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-subnav")).toBeNull();
  });

  it("gives an operator the real Providers page, not a placeholder", () => {
    renderSettings({ page: "providers" });
    expect(screen.getByTestId("providers-page")).toBeInTheDocument();
    expect(screen.getByTestId("settings-page-providers").textContent).not.toMatch(/coming soon/i);
  });

  it("keeps the Providers page away from a non-operator", () => {
    renderSettings({ page: "providers", role: "viewer" });
    expect(screen.queryByTestId("providers-page")).toBeNull();
    expect(screen.getByTestId("settings-restricted")).toBeInTheDocument();
    // Not even the listing request is made for someone who may not see it.
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("decides nothing while the session is still loading", () => {
    renderSettings({ role: undefined, roleLoading: true });
    expect(screen.queryByTestId("settings-restricted")).toBeNull();
    expect(screen.queryByTestId("settings-subnav")).toBeNull();
    expect(screen.getByTestId("settings-loading")).toBeInTheDocument();
  });
});
