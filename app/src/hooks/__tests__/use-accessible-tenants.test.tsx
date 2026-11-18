import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const mockSession: { data: Record<string, unknown> | null } = { data: null };

vi.mock("next-auth/react", () => ({
  useSession: () => ({
    data: mockSession.data,
    status: mockSession.data ? "authenticated" : "loading",
    update: vi.fn(),
  }),
}));

import { useHasChildTenants } from "../use-accessible-tenants";

const PARENT = { id: "acme-parent", display_name: "Acme Holdings", parent_tenant_id: null, is_home: true };
const CHILD = { id: "acme-child-a", display_name: "Acme Child A", parent_tenant_id: "acme-parent", is_home: false };

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

beforeEach(() => {
  mockSession.data = { role: "owner", tenantId: "acme-parent", homeTenantId: "acme-parent" };
});

afterEach(() => vi.restoreAllMocks());

describe("useHasChildTenants", () => {
  it("is true for an operator at home whose listing has more than its home", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse([PARENT, CHILD]));

    const { result } = renderHook(() => useHasChildTenants());

    await waitFor(() => expect(result.current).toBe(true));
  });

  it("is false when the listing holds only the home tenant", async () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse([PARENT]));

    const { result } = renderHook(() => useHasChildTenants());

    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });

  it("is false while acting in another tenant", async () => {
    mockSession.data = { role: "owner", tenantId: "acme-child-a", homeTenantId: "acme-parent" };
    vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse([PARENT, CHILD]));

    const { result } = renderHook(() => useHasChildTenants());

    await new Promise((r) => setTimeout(r, 20));
    expect(result.current).toBe(false);
  });

  it("does not ask for a member, who never has child tenants", async () => {
    mockSession.data = { role: "member", tenantId: "acme-parent", homeTenantId: "acme-parent" };
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse([PARENT, CHILD]));

    const { result } = renderHook(() => useHasChildTenants());

    await new Promise((r) => setTimeout(r, 20));
    expect(spy).not.toHaveBeenCalled();
    expect(result.current).toBe(false);
  });

  it("is false when the listing is refused", async () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(jsonResponse({ error: "no" }, 403));

    const { result } = renderHook(() => useHasChildTenants());

    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(result.current).toBe(false);
  });
});
