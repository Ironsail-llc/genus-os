import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RuntimeConfigProvider } from "@/components/runtime-config";
import { MarketplaceView } from "@/components/views/marketplace-view";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

describe("MarketplaceView", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ agents: [] }),
    });
  });

  it("keeps Bridge requests on the authenticated same-origin BFF", async () => {
    render(<MarketplaceView visible />);

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith("/api/bridge/api/installed-agents");
    });
  });

  it("links to the live marketplace by default", async () => {
    render(<MarketplaceView visible />);

    expect(screen.getByRole("link", { name: /Browse Marketplace/ })).toHaveAttribute(
      "href",
      "https://programmaticresources.io/browse",
    );
    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
  });

  it("links installed agents to the configured marketplace", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        agents: [
          {
            agent_id: "email-triage",
            version: "1.0.0",
            installed_at: "2026-10-01T00:00:00Z",
            source: "hub",
            department: "ops",
            has_manifest: true,
          },
        ],
      }),
    });

    render(
      <RuntimeConfigProvider aiName="Genus" marketplaceUrl="https://hub.example">
        <MarketplaceView visible />
      </RuntimeConfigProvider>,
    );

    expect(screen.getByRole("link", { name: /Browse Marketplace/ })).toHaveAttribute(
      "href",
      "https://hub.example/browse",
    );
    expect(await screen.findByTitle("View on Marketplace")).toHaveAttribute(
      "href",
      "https://hub.example/bundle/email-triage",
    );
  });
});
