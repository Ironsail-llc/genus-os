import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
});
