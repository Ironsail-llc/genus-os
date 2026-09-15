import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SettingsView } from "../settings-view";

// Config and Flags are real pages that read the settings schema on mount. This
// suite is about the CONTAINER, so they are stubbed — their own suites drive
// them, and a 164 KB schema fetch here would prove nothing about the sub-nav.
vi.mock("@/components/views/settings/config-page", () => ({
  ConfigPage: ({ visible, onOpenFlags }: { visible?: boolean; onOpenFlags?: () => void }) =>
    visible ? <button data-testid="config-page" onClick={() => onOpenFlags?.()} /> : null,
}));

vi.mock("@/components/views/settings/flags-page", () => ({
  FlagsPage: ({ visible, role }: { visible?: boolean; role?: string | null }) =>
    visible ? <div data-testid="flags-page" data-role={role ?? ""} /> : null,
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
    // Secrets is still a placeholder; Plugins stopped being one when the real
    // screen landed, and the assertion moved rather than being deleted.
    renderSettings({ page: "secrets" });
    const page = screen.getByTestId("settings-page-secrets");
    expect(page.textContent).toMatch(/soon/i);
    expect(page.textContent!.length).toBeGreaterThan(30);
  });

  it("gives an operator the real Plugins page, not a placeholder", () => {
    renderSettings({ page: "plugins" });
    expect(screen.getByTestId("settings-page-plugins").textContent).not.toMatch(/coming soon/i);
    expect(screen.getByTestId("plugins-refresh")).toBeInTheDocument();
  });

  it("keeps the Plugins page away from a non-operator, request and all", () => {
    renderSettings({ page: "plugins", role: "member" });
    expect(screen.queryByTestId("plugins-refresh")).toBeNull();
    expect(screen.getByTestId("settings-restricted")).toBeInTheDocument();
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("changes page on click", () => {
    const onPageChange = vi.fn();
    renderSettings({ onPageChange });
    fireEvent.click(screen.getByTestId("settings-nav-plugins"));
    expect(onPageChange).toHaveBeenCalledWith("plugins");
  });

  it("gives the Flags page the governed-flags screen, with the session's role", () => {
    renderSettings({ page: "flags" });
    expect(screen.getByTestId("flags-page")).toHaveAttribute("data-role", "owner");
  });

  it("gives the Config page the schema-driven form", () => {
    renderSettings({ page: "config" });
    expect(screen.getByTestId("config-page")).toBeInTheDocument();
    expect(screen.queryByText(/coming soon/i)).toBeNull();
  });

  it("lets the Config page send the operator to Flags for a verdict", () => {
    // A governed field is editable on both screens, but only Flags says what
    // the control is actually DOING — so the link has to go somewhere.
    const onPageChange = vi.fn();
    renderSettings({ page: "config", onPageChange });
    fireEvent.click(screen.getByTestId("config-page"));
    expect(onPageChange).toHaveBeenCalledWith("flags");
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

  it("gives an operator the real Channels and Users pages, not placeholders", () => {
    renderSettings({ page: "channels" });
    expect(screen.getByTestId("channels-page")).toBeInTheDocument();
    expect(screen.getByTestId("settings-page-channels").textContent).not.toMatch(/coming soon/i);

    renderSettings({ page: "users" });
    expect(screen.getByTestId("users-page")).toBeInTheDocument();
    expect(screen.getByTestId("settings-page-users").textContent).not.toMatch(/coming soon/i);
  });

  it("keeps Channels and Users away from a non-operator, request and all", () => {
    renderSettings({ page: "users", role: "member" });
    expect(screen.queryByTestId("users-page")).toBeNull();
    expect(screen.getByTestId("settings-restricted")).toBeInTheDocument();
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
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
