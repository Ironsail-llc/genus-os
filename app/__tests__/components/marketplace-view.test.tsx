import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

/**
 * A trust badge on every installed agent. An agent a plugin ships takes its
 * plugin's trust — signature, scan and drift belong to the wheel it came in —
 * and any other agent is Unsigned with the sha256 it was pinned to, because no
 * index vouched for it. `/api/plugins` is operator-only; when it refuses, the
 * plugin's agents still render, as Unsigned rather than not at all.
 */
describe("MarketplaceView — trust", () => {
  const AGENTS = {
    agents: [
      {
        agent_id: "desk-watch",
        version: "0.3.0",
        installed_at: "",
        source: "plugin:genus-example",
        plugin: "genus-example",
        department: "ops",
        has_manifest: true,
      },
      {
        agent_id: "note-taker",
        version: "1.0.0",
        installed_at: "",
        source: "hub",
        source_sha256: "d".repeat(64),
        department: "",
        has_manifest: true,
      },
    ],
  };
  const PLUGINS = {
    plugins: [
      {
        name: "genus-example",
        verdict: "safe",
        recorded: true,
        drifted: false,
        trust: {
          signature: { verified: true, key_id: "k1", index_url: null },
          wheel: { sha256: "b".repeat(64), pinned: true },
          manifest: { sha256: "c".repeat(64), matches: true },
          scan: { verdict: "safe", reasons: [], members_accounted: 3 },
          lockfile: { recorded: true, drifted: false },
          contract_version: 1,
        },
      },
    ],
  };

  function routes(plugins: { ok: boolean; status: number; body: unknown }) {
    mockFetch.mockImplementation(async (url: string) => {
      if (url.endsWith("/api/plugins")) {
        return { ok: plugins.ok, status: plugins.status, json: async () => plugins.body };
      }
      return { ok: true, status: 200, json: async () => AGENTS };
    });
  }

  it("gives a plugin-shipped agent its plugin's badge and a hub agent Unsigned", async () => {
    routes({ ok: true, status: 200, body: PLUGINS });
    render(<MarketplaceView visible />);

    await waitFor(() =>
      expect(screen.getByTestId("agent-trust-desk-watch")).toHaveAttribute(
        "data-level",
        "verified"
      )
    );
    expect(screen.getByTestId("agent-trust-note-taker")).toHaveAttribute("data-level", "unsigned");
  });

  it("shows the pinned sha256 in a hub agent's drawer", async () => {
    routes({ ok: true, status: 200, body: PLUGINS });
    render(<MarketplaceView visible />);

    fireEvent.click(await screen.findByTestId("agent-trust-note-taker"));
    expect(screen.getByTestId("trust-wheel").textContent).toContain("d".repeat(64));
  });

  it("still renders every agent when the plugin listing is refused", async () => {
    routes({ ok: false, status: 403, body: { detail: "operator only" } });
    render(<MarketplaceView visible />);

    expect(await screen.findByTestId("agent-trust-desk-watch")).toHaveAttribute(
      "data-level",
      "unsigned"
    );
    expect(screen.getByTestId("installed-agent-note-taker")).toBeInTheDocument();
  });
});
