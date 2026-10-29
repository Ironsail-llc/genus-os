import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockSession: { data: Record<string, unknown> | null; update: ReturnType<typeof vi.fn> } = {
  data: null,
  update: vi.fn(),
};

vi.mock("next-auth/react", () => ({
  useSession: () => ({
    data: mockSession.data,
    status: mockSession.data ? "authenticated" : "loading",
    update: mockSession.update,
  }),
}));

import { ActingTenantBanner, TenantSwitcher } from "@/components/layout/tenant-switcher";
import { actingTenantId } from "@/lib/tenant-session";

const PARENT = { id: "acme-parent", display_name: "Acme Parent", parent_tenant_id: null, is_home: true };
const CHILD = {
  id: "acme-child-a",
  display_name: "Acme Child A",
  parent_tenant_id: "acme-parent",
  is_home: false,
};

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function routeFetch(routes: Record<string, () => Response>) {
  return vi.spyOn(global, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    for (const [path, reply] of Object.entries(routes)) {
      if (url.endsWith(path)) return reply();
    }
    return jsonResponse({ error: "unexpected" }, 500);
  });
}

beforeEach(() => {
  mockSession.data = { role: "owner", tenantId: "acme-parent", homeTenantId: "acme-parent" };
  mockSession.update = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("actingTenantId", () => {
  it("is the acting tenant only when it differs from home", () => {
    expect(actingTenantId({ tenantId: "acme-child-a", homeTenantId: "acme-parent" })).toBe(
      "acme-child-a",
    );
    expect(actingTenantId({ tenantId: "acme-parent", homeTenantId: "acme-parent" })).toBeNull();
  });

  it("is never acting without both claims", () => {
    expect(actingTenantId({ tenantId: "acme-child-a" })).toBeNull();
    expect(actingTenantId({ homeTenantId: "acme-parent" })).toBeNull();
    expect(actingTenantId(null)).toBeNull();
    expect(actingTenantId(undefined)).toBeNull();
  });
});

describe("TenantSwitcher", () => {
  it("is not rendered for a user with a single accessible tenant", async () => {
    const fetchMock = routeFetch({ "/api/bridge/api/tenants/accessible": () => jsonResponse([PARENT]) });

    const { container } = render(<TenantSwitcher onSwitched={vi.fn()} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container.querySelector("select")).toBeNull();
    expect(screen.queryByTestId("tenant-switcher")).toBeNull();
  });

  it("is not rendered when the listing is refused", async () => {
    const fetchMock = routeFetch({
      "/api/bridge/api/tenants/accessible": () => jsonResponse({ error: "human user required" }, 403),
    });

    render(<TenantSwitcher onSwitched={vi.fn()} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByTestId("tenant-switcher")).toBeNull();
  });

  it("lists every accessible tenant with the acting one selected", async () => {
    mockSession.data = { role: "owner", tenantId: "acme-child-a", homeTenantId: "acme-parent" };
    routeFetch({ "/api/bridge/api/tenants/accessible": () => jsonResponse([PARENT, CHILD]) });

    render(<TenantSwitcher onSwitched={vi.fn()} />);

    const select = (await screen.findByLabelText(/tenant/i)) as HTMLSelectElement;
    expect(select.value).toBe("acme-child-a");
    expect([...select.options].map((o) => o.textContent)).toEqual(["Acme Parent", "Acme Child A"]);
  });

  it("switches through the session route and reloads on success", async () => {
    const fetchMock = routeFetch({
      "/api/bridge/api/tenants/accessible": () => jsonResponse([PARENT, CHILD]),
      "/api/session/switch-tenant": () =>
        jsonResponse({ tenant: "acme-child-a", homeTenant: "acme-parent" }),
    });
    const onSwitched = vi.fn();

    render(<TenantSwitcher onSwitched={onSwitched} />);
    fireEvent.change(await screen.findByLabelText(/tenant/i), { target: { value: "acme-child-a" } });

    await waitFor(() => expect(onSwitched).toHaveBeenCalledOnce());
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/session/switch-tenant"));
    expect(call?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ tenant: "acme-child-a" });
  });

  it("shows the refusal and keeps the current tenant when the switch is refused", async () => {
    routeFetch({
      "/api/bridge/api/tenants/accessible": () => jsonResponse([PARENT, CHILD]),
      "/api/session/switch-tenant": () => jsonResponse({ error: "tenant switching disabled" }, 403),
    });
    const onSwitched = vi.fn();

    render(<TenantSwitcher onSwitched={onSwitched} />);
    const select = (await screen.findByLabelText(/tenant/i)) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "acme-child-a" } });

    expect(await screen.findByRole("alert")).toHaveTextContent(/tenant switching disabled/i);
    expect(select.value).toBe("acme-parent");
    expect(onSwitched).not.toHaveBeenCalled();
  });
});

describe("ActingTenantBanner", () => {
  it("is absent while the session acts in its home tenant", () => {
    const fetchMock = routeFetch({});

    const { container } = render(<ActingTenantBanner onSwitched={vi.fn()} />);

    expect(container.textContent).toBe("");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("names the acting tenant and switches back to home", async () => {
    mockSession.data = { role: "owner", tenantId: "acme-child-a", homeTenantId: "acme-parent" };
    const fetchMock = routeFetch({
      "/api/bridge/api/tenants/accessible": () => jsonResponse([PARENT, CHILD]),
      "/api/session/switch-tenant": () =>
        jsonResponse({ tenant: "acme-parent", homeTenant: "acme-parent" }),
    });
    const onSwitched = vi.fn();

    render(<ActingTenantBanner onSwitched={onSwitched} />);

    const banner = await screen.findByTestId("acting-tenant-banner");
    await waitFor(() => expect(banner).toHaveTextContent("Acting as Acme Child A"));
    fireEvent.click(screen.getByRole("button", { name: /switch back/i }));

    await waitFor(() => expect(onSwitched).toHaveBeenCalledOnce());
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/session/switch-tenant"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ tenant: "acme-parent" });
  });

  it("falls back to the tenant id when the listing cannot name it", async () => {
    mockSession.data = { role: "owner", tenantId: "acme-child-a", homeTenantId: "acme-parent" };
    routeFetch({ "/api/bridge/api/tenants/accessible": () => jsonResponse({}, 502) });

    render(<ActingTenantBanner onSwitched={vi.fn()} />);

    expect(await screen.findByTestId("acting-tenant-banner")).toHaveTextContent(
      "Acting as acme-child-a",
    );
  });

  it("tells the operator once when a refresh dropped them back to home", async () => {
    mockSession.data = {
      role: "owner",
      tenantId: "acme-parent",
      homeTenantId: "acme-parent",
      tenantNotice: "acting-tenant-reverted",
    };
    routeFetch({});

    render(<ActingTenantBanner onSwitched={vi.fn()} />);

    expect(await screen.findByRole("status")).toHaveTextContent(/back in your home tenant/i);
    await waitFor(() => expect(mockSession.update).toHaveBeenCalledWith({ tenantNoticeSeen: true }));
  });
});
