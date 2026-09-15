/**
 * A settings sub-page that throws must cost the operator that page, not the
 * Helm.
 *
 * `app/src/app` carries no `error.tsx` and no `global-error.tsx`, so an
 * exception thrown while rendering a view unmounts the whole React tree and
 * leaves a blank window — with the sidebar, the chat and every other screen
 * gone. Settings pages read nested payloads from a separately-versioned engine,
 * which is exactly the shape that produces one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { SettingsView } from "../settings-view";

vi.mock("@/components/views/settings/config-page", () => ({
  ConfigPage: ({ visible }: { visible?: boolean }) =>
    visible ? <div data-testid="config-page" /> : null,
}));

vi.mock("@/components/views/settings/flags-page", () => ({
  FlagsPage: ({ visible }: { visible?: boolean }) =>
    visible ? <div data-testid="flags-page" /> : null,
}));

vi.mock("@/components/views/settings/providers-page", () => ({
  ProvidersPage: () => {
    throw new TypeError("Cannot read properties of undefined (reading 'map')");
  },
}));

beforeEach(() => {
  // React re-throws into console.error on its way to the boundary. The noise
  // is expected here and would otherwise bury a real failure.
  vi.spyOn(console, "error").mockImplementation(() => {});
  // Channels is a real page now and fetches on mount. A request that never
  // settles leaves it in its loading state, which is what this suite wants:
  // it is about the container recovering, not about that page's contents.
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Settings sub-page error boundary", () => {
  it("shows a panel instead of blanking the Helm", () => {
    render(
      <SettingsView visible page="providers" onPageChange={vi.fn()} role="owner" />
    );
    const panel = screen.getByTestId("settings-page-error");
    expect(panel.textContent).toMatch(/failed to render/i);
    expect(panel.textContent!.length).toBeGreaterThan(30);
  });

  it("leaves the rest of Settings usable", () => {
    const onPageChange = vi.fn();
    render(<SettingsView visible page="providers" onPageChange={onPageChange} role="owner" />);

    expect(screen.getByTestId("settings-subnav")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("settings-nav-channels"));
    expect(onPageChange).toHaveBeenCalledWith("channels");
  });

  it("does not keep a broken page's failure on the next page", () => {
    const { rerender } = render(
      <SettingsView visible page="providers" onPageChange={vi.fn()} role="owner" />
    );
    expect(screen.getByTestId("settings-page-error")).toBeInTheDocument();

    rerender(<SettingsView visible page="channels" onPageChange={vi.fn()} role="owner" />);
    expect(screen.queryByTestId("settings-page-error")).toBeNull();
    expect(screen.getByTestId("settings-page-channels")).toBeInTheDocument();
  });
});
