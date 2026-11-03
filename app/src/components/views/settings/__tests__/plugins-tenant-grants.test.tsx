/**
 * Settings › Plugins, per tenant.
 *
 * The listing is the ACTING tenant's engine (the bridge call follows the
 * token), and the root tenant may force a plugin on or off for a child:
 *
 * * **A forced plugin's switch is locked** and says who decided — "Set by the
 *   root tenant". The engine answers a toggle on one with 409, so a live
 *   switch would be a control whose only outcome is a refusal.
 * * **"disabled by the root tenant" is a decision, not a fault**, the same as
 *   "disabled by operator": muted, never red.
 * * **Only the root operator at home gets the per-child control.** A child
 *   admin, or a root admin who switched into a child, would get a 403 from the
 *   grant route; they are never offered it, and the accessible-tenant listing
 *   is not even read for them.
 * * **The control names the child and reports the reload.** A grant whose
 *   reload failed is saved but not live, and the page says so.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockSession: { data: Record<string, unknown> | null } = { data: null };

vi.mock("next-auth/react", () => ({
  useSession: () => ({
    data: mockSession.data,
    status: mockSession.data ? "authenticated" : "unauthenticated",
    update: vi.fn(),
  }),
}));

import { PluginsPage } from "../plugins-page";

const ROOT = "acme-parent";
const CHILD_A = "acme-child-a";
const CHILD_B = "acme-child-b";

function sessionFor(tenantId: string, homeTenantId: string) {
  return { role: "admin", tenantId, homeTenantId, rootTenantId: ROOT, backendAuthorized: true };
}

const BASE = {
  version: "0.1.0",
  recorded: true,
  verdict: "unscanned",
  drifted: false,
  groups: ["genus.tools"],
  manifest: null,
  source: null,
};

const FREE = {
  ...BASE,
  name: "genus-hostinfo",
  enabled: true,
  state: "loaded",
  contributions: { tools: 1 },
  failure_reason: null,
  forced: null,
};

const FORCED_OFF = {
  ...BASE,
  name: "genus-ledger",
  enabled: true,
  state: "disabled",
  contributions: {},
  failure_reason: "disabled by the root tenant",
  forced: "off",
};

const FORCED_ON = {
  ...BASE,
  name: "genus-notes",
  // The child's own row says off; the root's grant wins.
  enabled: false,
  state: "loaded",
  contributions: { tools: 1 },
  failure_reason: null,
  forced: "on",
};

const LISTING = {
  generation: 2,
  indexes: [],
  lockfile: { path_configured: true, present: true, malformed: false, rows: 3, problem: null },
  plugins: [FREE, FORCED_OFF, FORCED_ON],
};

const ACCESSIBLE = [
  { id: ROOT, display_name: "Acme Parent", parent_tenant_id: null, is_home: true },
  { id: CHILD_A, display_name: "Acme Child A", parent_tenant_id: ROOT, is_home: false },
  { id: CHILD_B, display_name: "Acme Child B", parent_tenant_id: ROOT, is_home: false },
];

interface Calls {
  urls: string[];
  puts: Array<{ url: string; body: Record<string, unknown> }>;
}

function mockBridge(options: { listStatus?: number; grant?: { status: number; body: unknown } } = {}) {
  const calls: Calls = { urls: [], puts: [] };
  vi.spyOn(global, "fetch").mockImplementation((async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ) => {
    const url = String(input);
    calls.urls.push(url);
    const method = init?.method ?? "GET";
    const reply = (status: number, body: unknown) =>
      ({ ok: status < 400, status, json: async () => body }) as Response;
    if (method === "PUT") {
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : {};
      calls.puts.push({ url, body });
      const tenant = decodeURIComponent(url.split("/tenants/")[1] ?? "");
      return reply(
        options.grant?.status ?? 200,
        options.grant?.body ?? {
          plugin: "genus-hostinfo",
          tenant_id: tenant,
          state: body.state,
          reloaded: true,
          reload_status: 200,
          reload: { generation: 5, loaded: 1, failures: [] },
        },
      );
    }
    if (url.endsWith("/api/tenants/accessible")) return reply(200, ACCESSIBLE);
    const status = options.listStatus ?? 200;
    return reply(status, status < 400 ? LISTING : { detail: "tenant administrator role required" });
  }) as typeof fetch);
  return calls;
}

beforeEach(() => {
  mockSession.data = sessionFor(CHILD_A, CHILD_A);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("a plugin the root tenant forced", () => {
  it("locks the switch at off and says who decided", async () => {
    mockBridge();
    render(<PluginsPage visible />);

    const card = await screen.findByTestId("plugin-genus-ledger");
    const toggle = within(card).getByTestId("plugin-switch-genus-ledger");
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(within(card).getByTestId("plugin-forced-genus-ledger").textContent).toContain(
      "Set by the root tenant",
    );
  });

  it("locks the switch at on although the child's own row says off", async () => {
    mockBridge();
    render(<PluginsPage visible />);

    const card = await screen.findByTestId("plugin-genus-notes");
    const toggle = within(card).getByTestId("plugin-switch-genus-notes");
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(within(card).getByTestId("plugin-forced-genus-notes").textContent).toContain(
      "Set by the root tenant",
    );
  });

  it("renders the root's refusal as a decision, not a fault", async () => {
    mockBridge();
    render(<PluginsPage visible />);

    const reason = await screen.findByTestId("plugin-failure-genus-ledger");
    expect(reason.className).toContain("muted");
    expect(reason.className).not.toContain("destructive");
  });

  it("leaves an unforced plugin's switch alone", async () => {
    mockBridge();
    render(<PluginsPage visible />);

    const card = await screen.findByTestId("plugin-genus-hostinfo");
    expect(within(card).getByTestId("plugin-switch-genus-hostinfo")).toBeEnabled();
    expect(within(card).queryByTestId("plugin-forced-genus-hostinfo")).toBeNull();
  });
});

describe("who gets the per-child control", () => {
  it("is never offered to a child admin, and the tenant tree is not read", async () => {
    const calls = mockBridge();
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    expect(screen.queryByTestId("plugin-grant-genus-hostinfo")).toBeNull();
    expect(calls.urls.some((u) => u.endsWith("/api/tenants/accessible"))).toBe(false);
  });

  it("is never offered to a root admin acting in a child", async () => {
    mockSession.data = sessionFor(CHILD_A, ROOT);
    const calls = mockBridge();
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    expect(screen.queryByTestId("plugin-grant-genus-hostinfo")).toBeNull();
    expect(calls.urls.some((u) => u.endsWith("/api/tenants/accessible"))).toBe(false);
  });

  it("is offered to the root operator at home, with the children to pick from", async () => {
    mockSession.data = sessionFor(ROOT, ROOT);
    mockBridge();
    render(<PluginsPage visible />);

    const control = await screen.findByTestId("plugin-grant-genus-hostinfo");
    const picker = await within(control).findByTestId("plugin-grant-tenant-genus-hostinfo");
    await waitFor(() => expect(picker.querySelectorAll("option").length).toBe(2));
    const options = [...picker.querySelectorAll("option")].map((o) => o.getAttribute("value"));
    // Children only: the root decides for itself with the switch.
    expect(options).toEqual([CHILD_A, CHILD_B]);
  });
});

describe("forcing a plugin for a child", () => {
  beforeEach(() => {
    mockSession.data = sessionFor(ROOT, ROOT);
  });

  async function control(name = "genus-hostinfo") {
    const box = await screen.findByTestId(`plugin-grant-${name}`);
    const picker = within(box).getByTestId(`plugin-grant-tenant-${name}`);
    await waitFor(() => expect(picker.querySelectorAll("option").length).toBe(2));
    return { box, picker };
  }

  it.each([
    ["plugin-grant-off-genus-hostinfo", "off"],
    ["plugin-grant-on-genus-hostinfo", "on"],
    ["plugin-grant-clear-genus-hostinfo", null],
  ])("%s writes the grant for the picked child", async (button, state) => {
    const calls = mockBridge();
    render(<PluginsPage visible />);
    const { box, picker } = await control();

    fireEvent.change(picker, { target: { value: CHILD_B } });
    fireEvent.click(within(box).getByTestId(button));

    await waitFor(() => expect(calls.puts).toHaveLength(1));
    expect(calls.puts[0].url).toBe(`/api/bridge/api/plugins/genus-hostinfo/tenants/${CHILD_B}`);
    expect(calls.puts[0].body).toEqual({ state });
    const result = await within(box).findByTestId("plugin-grant-result-genus-hostinfo");
    expect(result.textContent).toContain(CHILD_B);
    expect(result.textContent).toContain("reloaded");
  });

  it("says a grant whose reload failed is saved but not live", async () => {
    mockBridge({
      grant: {
        status: 200,
        body: {
          plugin: "genus-hostinfo",
          tenant_id: CHILD_A,
          state: "off",
          reloaded: false,
          reload_status: 502,
          reload: { error: "engine unreachable" },
        },
      },
    });
    render(<PluginsPage visible />);
    const { box } = await control();

    fireEvent.click(within(box).getByTestId("plugin-grant-off-genus-hostinfo"));

    const result = await within(box).findByTestId("plugin-grant-result-genus-hostinfo");
    expect(result.textContent).toContain("saved");
    expect(result.textContent).toContain("could not be reloaded");
    expect(result.className).toContain("warning");
  });

  it("shows a refused grant in the bridge's words", async () => {
    mockBridge({ grant: { status: 404, body: { detail: "no such tenant" } } });
    render(<PluginsPage visible />);
    const { box } = await control();

    fireEvent.click(within(box).getByTestId("plugin-grant-off-genus-hostinfo"));

    const error = await within(box).findByTestId("plugin-grant-error-genus-hostinfo");
    expect(error.textContent).toContain("no such tenant");
  });
});

describe("what is installed stays root", () => {
  const WITH_SOURCE = {
    ...LISTING,
    plugins: [
      {
        ...FREE,
        source: {
          origin: "registry",
          installed_at: "2026-09-15T10:00:00+00:00",
          index_url: "https://plugins.example.org/index.json",
          publisher_key_id: "genus-2026",
        },
      },
    ],
  };

  function listing(body: unknown) {
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      const payload = url.endsWith("/api/tenants/accessible") ? ACCESSIBLE : body;
      return { ok: true, status: 200, json: async () => payload } as Response;
    }) as typeof fetch);
  }

  it.each([
    ["a child admin", () => sessionFor(CHILD_A, CHILD_A)],
    ["a root admin acting in a child", () => sessionFor(CHILD_A, ROOT)],
  ])("%s is not offered install, record or remove", async (_who, session) => {
    mockSession.data = session();
    listing(WITH_SOURCE);
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    expect(screen.queryByTestId("plugins-install-card")).toBeNull();
    expect(screen.queryByTestId("plugins-record")).toBeNull();
    expect(screen.queryByTestId("plugin-remove-genus-hostinfo")).toBeNull();
    // Their own acts are still there.
    expect(screen.getByTestId("plugins-reload")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-switch-genus-hostinfo")).toBeEnabled();
  });

  it("tells a child admin on a fresh box who records plugins", async () => {
    listing({
      ...LISTING,
      lockfile: { path_configured: true, present: false, malformed: false, rows: 0, problem: null },
    });
    render(<PluginsPage visible />);

    const note = await screen.findByTestId("plugins-record-root-only");
    expect(note.textContent).toContain("root administrator");
    expect(screen.queryByTestId("plugins-record")).toBeNull();
  });

  it("the root operator at home still gets all three", async () => {
    mockSession.data = sessionFor(ROOT, ROOT);
    listing(WITH_SOURCE);
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    expect(screen.getByTestId("plugins-install-card")).toBeInTheDocument();
    expect(screen.getByTestId("plugins-record")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-remove-genus-hostinfo")).toBeInTheDocument();
  });
});

describe("a refused listing", () => {
  it("explains that plugins belong to each tenant's owners and admins", async () => {
    mockBridge({ listStatus: 403 });
    render(<PluginsPage visible />);

    const forbidden = await screen.findByTestId("plugins-forbidden");
    expect(forbidden.textContent).toContain("owner or admin of this tenant");
    expect(forbidden.textContent).not.toContain("platform tenant");
  });
});
