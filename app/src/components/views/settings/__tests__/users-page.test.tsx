/**
 * Settings › Users & roles, as the operator drives it.
 *
 * The shapes are the bridge's own (`crm/bridge/routers/users.py`): the roles
 * listing, the account listing, the invite reply and its one-shot grant, and
 * the refusal sentences the owner rules answer with.
 *
 * Three claims are load-bearing here:
 *
 * * nothing secret-shaped is ever rendered, even if the API grew a field —
 *   the row is assembled from a fixed set of keys, so an unknown key cannot
 *   reach the screen;
 * * a refusal is rendered in the SERVER's words. "Only an owner may…" and
 *   "this is the tenant's only owner" are instructions, and replacing either
 *   with "something went wrong" throws away the only thing the operator needs;
 * * the grant is shown ONCE, with its expiry and issuer, and it carries no
 *   secret — so what the page has to say is what the invitee must DO.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UsersPage } from "../users-page";

const ROLES = {
  roles: [
    { id: "admin", description: "Administers the instance: users, agents, credentials, settings." },
    { id: "auditor", description: "Read-only, plus the audit log. For review, not operation." },
    { id: "member", description: "Everyday access — chat, the CRM, and the agents' output." },
    {
      id: "owner",
      description: "Runs this instance. Full access, and the only role the appliance requires exactly one of.",
    },
    { id: "viewer", description: "Read-only, plus chat. What a paired channel sender gets." },
  ],
};

const OWNER = {
  id: "11111111-1111-4111-8111-111111111111",
  email: "alice@example.com",
  display_name: "Alice",
  role: "owner",
  status: "active",
  sso_bound: true,
  mfa_enabled: true,
  last_login_at: "2026-06-15T08:00:00+00:00",
  created_at: "2026-01-02T09:00:00+00:00",
};

const MEMBER = {
  id: "22222222-2222-4222-8222-222222222222",
  email: "bob@example.com",
  display_name: "Bob",
  role: "member",
  status: "active",
  sso_bound: false,
  mfa_enabled: false,
  last_login_at: null,
  created_at: "2026-05-02T09:00:00+00:00",
};

const USERS = { users: [OWNER, MEMBER], count: 2 };

interface Call {
  url: string;
  method: string;
  body: unknown;
}

interface Bridge {
  calls: Call[];
  fetchMock: ReturnType<typeof vi.fn>;
  reply: (fragment: string, status: number, body: unknown, method?: string) => void;
}

function mockBridge(users: unknown = USERS, roles: unknown = ROLES): Bridge {
  const calls: Call[] = [];
  const overrides: Array<{ fragment: string; method?: string; status: number; body: unknown }> = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });

    const answer = (status: number, body: unknown) =>
      ({ ok: status < 400, status, json: async () => body }) as Response;
    const override = overrides.find(
      (entry) => url.includes(entry.fragment) && (!entry.method || entry.method === method)
    );
    if (override) return answer(override.status, override.body);

    if (url.includes("/api/auth/roles")) return answer(200, roles);
    if (url.includes("/binding-grant")) {
      return answer(201, {
        grant: {
          id: "44444444-4444-4444-8444-444444444444",
          expires_at: "2026-06-15T13:00:00+00:00",
          issuer: "https://idp.example.com",
        },
      });
    }
    if (method === "POST" && url.endsWith("/api/users")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      return answer(201, {
        user: {
          ...MEMBER,
          id: "33333333-3333-4333-8333-333333333333",
          email: body.email,
          display_name: body.display_name ?? "Carol",
          role: body.role,
          status: body.sso ? "active" : "invited",
        },
        ...(body.sso
          ? {
              grant: {
                id: "55555555-5555-4555-8555-555555555555",
                expires_at: "2026-06-15T13:00:00+00:00",
                issuer: "https://idp.example.com",
              },
            }
          : {}),
      });
    }
    if (method === "PATCH") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      return answer(200, { user: { ...MEMBER, ...body } });
    }
    return answer(200, users);
  });

  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    fetchMock,
    reply: (fragment, status, body, method) =>
      overrides.unshift({ fragment, method: method?.toUpperCase(), status, body }),
  };
}

function callsTo(calls: Call[], fragment: string, method = "POST") {
  return calls.filter((call) => call.url.includes(fragment) && call.method === method);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-06-15T12:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderPage(props: Partial<React.ComponentProps<typeof UsersPage>> = {}) {
  render(<UsersPage visible role="owner" {...props} />);
  await screen.findByTestId(`user-row-${MEMBER.id}`);
}

describe("UsersPage", () => {
  it("lists every account with the facts the operator administers by", async () => {
    mockBridge();
    await renderPage();

    const row = screen.getByTestId(`user-row-${MEMBER.id}`);
    expect(row.textContent).toContain("bob@example.com");
    expect(row.textContent).toContain("Bob");
    expect(screen.getByTestId(`user-status-${MEMBER.id}`).textContent).toMatch(/active/i);
    expect(screen.getByTestId(`user-sso-${MEMBER.id}`).textContent).toMatch(/not bound|no/i);
    expect(screen.getByTestId(`user-sso-${OWNER.id}`).textContent).toMatch(/bound|yes/i);
    expect(screen.getByTestId(`user-mfa-${OWNER.id}`).textContent).toMatch(/on|yes|enabled/i);
    expect(screen.getByTestId(`user-last-login-${MEMBER.id}`).textContent).toMatch(/never/i);
  });

  it("offers every role the bridge listed, with its description as help", async () => {
    mockBridge();
    await renderPage();

    const select = screen.getByTestId(`user-role-${MEMBER.id}`) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual(
      ROLES.roles.map((role) => role.id)
    );
    expect(select.value).toBe("member");
    expect(screen.getByTestId(`user-role-help-${MEMBER.id}`).textContent).toContain(
      "Everyday access"
    );
  });

  it("never renders a secret-shaped field, even one the API should not send", async () => {
    mockBridge({
      users: [
        {
          ...MEMBER,
          password_hash: "$2b$12$notarealhashbutstillahash",
          mfa_secret_enc: "enc:notarealsecret",
          idp_subject: "idp-subject-0001",
        },
      ],
      count: 1,
    });
    await renderPage();

    const row = screen.getByTestId(`user-row-${MEMBER.id}`);
    expect(row.textContent).not.toContain("$2b$12$");
    expect(row.textContent).not.toContain("enc:notarealsecret");
    expect(row.textContent).not.toContain("idp-subject-0001");
    expect(document.body.textContent).not.toContain("idp-subject-0001");
  });

  it("posts exactly the invite body the route takes", async () => {
    const bridge = mockBridge();
    await renderPage();

    fireEvent.click(screen.getByTestId("users-invite-open"));
    fireEvent.change(screen.getByTestId("invite-email"), {
      target: { value: "carol@example.com" },
    });
    fireEvent.change(screen.getByTestId("invite-display-name"), { target: { value: "Carol" } });
    fireEvent.change(screen.getByTestId("invite-role"), { target: { value: "admin" } });
    fireEvent.click(screen.getByTestId("invite-sso"));
    fireEvent.click(screen.getByTestId("invite-submit"));

    await waitFor(() => expect(callsTo(bridge.calls, "/api/users")).toHaveLength(1));
    expect(callsTo(bridge.calls, "/api/users")[0].body).toEqual({
      email: "carol@example.com",
      role: "admin",
      display_name: "Carol",
      sso: true,
    });
  });

  it("shows the grant once, with its expiry, its issuer and what the invitee must do", async () => {
    mockBridge();
    await renderPage();

    fireEvent.click(screen.getByTestId("users-invite-open"));
    fireEvent.change(screen.getByTestId("invite-email"), {
      target: { value: "carol@example.com" },
    });
    fireEvent.click(screen.getByTestId("invite-sso"));
    fireEvent.click(screen.getByTestId("invite-submit"));

    const grant = await screen.findByTestId("invite-grant");
    expect(grant.textContent).toContain("https://idp.example.com");
    expect(grant.textContent).toContain("carol@example.com");
    expect(grant.textContent).toMatch(/sign in/i);
    // No secret exists to show, and the page must not imply one does.
    expect(grant.textContent).not.toMatch(/copy this (code|token|secret)/i);
    expect(screen.getByTestId("invite-grant-copy")).toBeTruthy();

    // Dismissed, it is gone — the grant is shown once.
    fireEvent.click(screen.getByTestId("invite-grant-dismiss"));
    expect(screen.queryByTestId("invite-grant")).toBeNull();
  });

  it("says a non-SSO invite has no grant and must be finished on the box", async () => {
    mockBridge();
    await renderPage();

    fireEvent.click(screen.getByTestId("users-invite-open"));
    fireEvent.change(screen.getByTestId("invite-email"), {
      target: { value: "carol@example.com" },
    });
    fireEvent.click(screen.getByTestId("invite-submit"));

    const note = await screen.findByTestId("invite-created");
    expect(note.textContent).toContain("genus user set-password");
    expect(screen.queryByTestId("invite-grant")).toBeNull();
  });

  it("renders a duplicate-address refusal in the bridge's own words", async () => {
    const bridge = mockBridge();
    bridge.reply("/api/users", 409, {
      detail: "an account with that address already exists",
    }, "POST");
    await renderPage();

    fireEvent.click(screen.getByTestId("users-invite-open"));
    fireEvent.change(screen.getByTestId("invite-email"), { target: { value: "bob@example.com" } });
    fireEvent.click(screen.getByTestId("invite-submit"));

    expect((await screen.findByTestId("invite-error")).textContent).toBe(
      "an account with that address already exists"
    );
  });

  it("renders an owner-rule refusal on a role change verbatim", async () => {
    const bridge = mockBridge();
    bridge.reply("/api/users/", 403, { detail: "only an owner may grant the owner role" }, "PATCH");
    await renderPage();

    fireEvent.change(screen.getByTestId(`user-role-${MEMBER.id}`), {
      target: { value: "owner" },
    });

    expect((await screen.findByTestId(`user-error-${MEMBER.id}`)).textContent).toBe(
      "only an owner may grant the owner role"
    );
  });

  it("does not retry a 409 and does not offer owner to a non-owner", async () => {
    const bridge = mockBridge();
    bridge.reply(
      "/api/users/",
      409,
      { detail: "this is the tenant's only owner — an instance with no owner cannot be administered." },
      "PATCH"
    );
    render(<UsersPage visible role="admin" />);
    await screen.findByTestId(`user-row-${MEMBER.id}`);

    const select = screen.getByTestId(`user-role-${MEMBER.id}`) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).not.toContain("owner");

    fireEvent.change(select, { target: { value: "viewer" } });
    expect((await screen.findByTestId(`user-error-${MEMBER.id}`)).textContent).toContain(
      "only owner"
    );
    await vi.advanceTimersByTimeAsync(5_000);
    expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}`, "PATCH")).toHaveLength(1);
  });

  it("changes a role with a PATCH carrying only the role", async () => {
    const bridge = mockBridge();
    await renderPage();

    fireEvent.change(screen.getByTestId(`user-role-${MEMBER.id}`), { target: { value: "viewer" } });
    await waitFor(() =>
      expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}`, "PATCH")).toHaveLength(1)
    );
    expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}`, "PATCH")[0].body).toEqual({
      role: "viewer",
    });
  });

  it("deactivates only after an inline confirmation, and never through a browser dialog", async () => {
    const bridge = mockBridge();
    const confirmSpy = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmSpy);
    await renderPage();

    fireEvent.click(screen.getByTestId(`user-deactivate-${MEMBER.id}`));
    expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}`, "PATCH")).toHaveLength(0);

    fireEvent.click(screen.getByTestId(`user-deactivate-confirm-${MEMBER.id}`));
    await waitFor(() =>
      expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}`, "PATCH")).toHaveLength(1)
    );
    expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}`, "PATCH")[0].body).toEqual({
      status: "disabled",
    });
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("arms a fresh SSO grant for an existing account", async () => {
    const bridge = mockBridge();
    await renderPage();

    fireEvent.click(screen.getByTestId(`user-grant-arm-${MEMBER.id}`));
    await waitFor(() =>
      expect(callsTo(bridge.calls, `/api/users/${MEMBER.id}/binding-grant`)).toHaveLength(1)
    );
    const grant = await screen.findByTestId(`user-grant-${MEMBER.id}`);
    expect(grant.textContent).toContain("https://idp.example.com");
    expect(grant.textContent).toContain("bob@example.com");
  });

  it("renders the grant refusal for an already-bound account verbatim", async () => {
    const bridge = mockBridge();
    bridge.reply("/binding-grant", 409, {
      detail:
        "that account cannot consume a binding grant — it is already bound to an identity provider, or it is not active",
    });
    await renderPage();

    fireEvent.click(screen.getByTestId(`user-grant-arm-${MEMBER.id}`));
    expect((await screen.findByTestId(`user-error-${MEMBER.id}`)).textContent).toContain(
      "already bound to an identity provider"
    );
  });

  it("says so when the roles listing fails, and still lists the accounts", async () => {
    const bridge = mockBridge();
    bridge.reply("/api/auth/roles", 503, { detail: "the account store is unavailable" });
    await renderPage();

    expect(screen.getByTestId("users-roles-error").textContent).toContain(
      "the account store is unavailable"
    );
    expect(screen.getByTestId(`user-row-${MEMBER.id}`)).toBeTruthy();
  });

  it("renders the bridge's own words when the listing fails", async () => {
    const bridge = mockBridge();
    bridge.reply("/api/users", 503, { detail: "the account store is unavailable" }, "GET");
    render(<UsersPage visible role="owner" />);
    expect((await screen.findByTestId("users-error")).textContent).toContain(
      "the account store is unavailable"
    );
  });

  it("says the instance has no accounts rather than showing an empty table", async () => {
    mockBridge({ users: [], count: 0 });
    render(<UsersPage visible role="owner" />);
    await screen.findByTestId("users-empty");
  });
});

describe("UsersPage polling", () => {
  it("does not touch the bridge while the page is hidden", () => {
    const { fetchMock } = mockBridge();
    render(<UsersPage visible={false} role="owner" />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-reads the accounts every 60 seconds while visible", async () => {
    const bridge = mockBridge();
    await renderPage();
    expect(callsTo(bridge.calls, "/api/users", "GET")).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(callsTo(bridge.calls, "/api/users", "GET")).toHaveLength(2));
  });

  it("stops polling once the page is hidden", async () => {
    const bridge = mockBridge();
    const { rerender } = render(<UsersPage visible role="owner" />);
    await screen.findByTestId(`user-row-${MEMBER.id}`);

    rerender(<UsersPage visible={false} role="owner" />);
    const before = callsTo(bridge.calls, "/api/users", "GET").length;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(callsTo(bridge.calls, "/api/users", "GET")).toHaveLength(before);
  });
});
