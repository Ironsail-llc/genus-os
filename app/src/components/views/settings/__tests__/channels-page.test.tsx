/**
 * Settings › Channels, as the operator drives it.
 *
 * Every shape here is the bridge's own, read off
 * `crm/bridge/routers/channel_access.py` — the listing, the verify reply and
 * the pairing/identity routes. Inventing a field in a fixture is how a page
 * ships against an API that does not exist, so nothing below is invented.
 *
 * The claims that matter are the ones about NOT lying:
 *
 * * `configured: null` is UNKNOWN, never "not configured" and never green;
 * * `pending_pairings: null` is "could not be read", never `0`;
 * * a verify that raised or hung is never rendered as a pass, whatever the
 *   steps array says;
 * * the page never offers a field that takes a credential.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChannelsPage } from "../channels-page";

const CHANNELS = {
  channels: [
    {
      name: "telegram",
      builtin: true,
      configured: true,
      health: {
        channel: "telegram",
        configured: true,
        bot_token: "sha256:aabbccddeeff",
        chat_id: "sha256:112233445566",
      },
      verify_available: true,
      access_mode: "pairing",
      pending_pairings: 2,
    },
    {
      name: "email",
      builtin: true,
      configured: false,
      health: { channel: "email", configured: false },
      verify_available: true,
      access_mode: "allowlist",
      pending_pairings: 0,
    },
    {
      name: "pager_relay",
      builtin: false,
      configured: null,
      health: { channel: "pager_relay", timed_out: true, error: "health() did not answer in 5s" },
      verify_available: false,
      access_mode: null,
      pending_pairings: null,
    },
  ],
};

const PENDING = {
  channel: "telegram",
  pending: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      channel: "telegram",
      expires_at: "2026-06-15T12:30:00+00:00",
      created_at: "2026-06-15T11:55:00+00:00",
      display_name_present: true,
    },
    {
      id: "22222222-2222-4222-8222-222222222222",
      channel: "telegram",
      expires_at: "2026-06-15T12:40:00+00:00",
      created_at: "2026-06-15T12:00:00+00:00",
      display_name_present: false,
    },
  ],
  count: 2,
};

const IDENTITIES = {
  channel: "telegram",
  identities: [
    {
      id: "33333333-3333-4333-8333-333333333333",
      user_id: "alice",
      native_id_fingerprint: "sha256:99887766",
      display_name: "Alice",
      role: "member",
      paired_at: "2026-06-01T09:00:00+00:00",
      paired_by: "operator:someone",
    },
  ],
  count: 1,
};

interface Call {
  url: string;
  method: string;
  body: unknown;
}

interface Bridge {
  calls: Call[];
  fetchMock: ReturnType<typeof vi.fn>;
  /** Replies keyed by a URL fragment; first match wins. */
  reply: (fragment: string, status: number, body: unknown, method?: string) => void;
}

function mockBridge(channels: unknown = CHANNELS): Bridge {
  const calls: Call[] = [];
  const overrides: Array<{ fragment: string; method?: string; status: number; body: unknown }> = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });

    const override = overrides.find(
      (entry) => url.includes(entry.fragment) && (!entry.method || entry.method === method)
    );
    const answer = (status: number, body: unknown) =>
      ({
        ok: status < 400,
        status,
        json: async () => body,
      }) as Response;

    if (override) return answer(override.status, override.body);
    if (url.includes("/pairings/") && url.endsWith("/approve")) {
      return answer(200, { id: IDENTITIES.identities[0].id, channel: "telegram", role: "viewer" });
    }
    if (url.includes("/pairings/") && url.endsWith("/deny")) {
      return answer(200, { denied: true, channel: "telegram" });
    }
    if (url.includes("/identities/")) return answer(200, { revoked: true, channel: "telegram" });
    if (url.endsWith("/identities")) return answer(200, IDENTITIES);
    if (url.endsWith("/pending")) return answer(200, PENDING);
    if (url.includes("/verify")) {
      return answer(200, {
        channel: "telegram",
        configured: true,
        verify_available: true,
        error_class: null,
        steps: [
          { step: "auth", ok: true, detail: "bot reachable" },
          { step: "post", ok: false, detail: "chat not found" },
        ],
      });
    }
    return answer(200, channels);
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

async function renderPage(props: Partial<React.ComponentProps<typeof ChannelsPage>> = {}) {
  render(<ChannelsPage visible {...props} />);
  await screen.findByTestId("channel-card-telegram");
}

describe("ChannelsPage", () => {
  it("shows one card per channel with its origin, state and access mode", async () => {
    mockBridge();
    await renderPage();

    expect(screen.getByTestId("channel-builtin-telegram").textContent).toMatch(/built-in/i);
    expect(screen.getByTestId("channel-builtin-pager_relay").textContent).toMatch(/plugin/i);
    expect(screen.getByTestId("channel-configured-telegram").textContent).toMatch(/configured/i);
    expect(screen.getByTestId("channel-access-mode-telegram").textContent).toMatch(/pairing/i);
    expect(screen.getByTestId("channel-access-mode-pager_relay").textContent).toMatch(/unknown/i);
  });

  it("paints configured:null as unknown — never as configured, never as not configured", async () => {
    mockBridge();
    await renderPage();

    const unknown = screen.getByTestId("channel-configured-pager_relay");
    expect(unknown.textContent).toMatch(/unknown/i);
    expect(unknown.textContent).not.toMatch(/^configured$/i);
    expect(unknown.textContent).not.toMatch(/not configured/i);

    // And the false case still reads as its own third state.
    expect(screen.getByTestId("channel-configured-email").textContent).toMatch(/not configured/i);
  });

  it("names the error class when the channel's own health report failed", async () => {
    mockBridge();
    await renderPage();
    expect(screen.getByTestId("channel-health-pager_relay").textContent).toContain("timed_out");
    expect(screen.getByTestId("channel-health-pager_relay").textContent).toContain(
      "health() did not answer in 5s"
    );
  });

  it("renders the health report's redacted fields exactly as the API sent them", async () => {
    mockBridge();
    await renderPage();
    const health = screen.getByTestId("channel-health-telegram").textContent ?? "";
    expect(health).toContain("sha256:aabbccddeeff");
    expect(health).toContain("sha256:112233445566");
  });

  it("does not report an unreadable pending count as nobody waiting", async () => {
    mockBridge();
    await renderPage();
    expect(screen.getByTestId("channel-pending-count-telegram").textContent).toContain("2");
    expect(screen.getByTestId("channel-pending-count-pager_relay").textContent).not.toMatch(/\b0\b/);
    expect(screen.getByTestId("channel-pending-count-pager_relay").textContent).toMatch(
      /could not be read/i
    );
  });

  it("says once how a credential is added, and offers no field that takes one", async () => {
    mockBridge();
    await renderPage();

    expect(screen.getAllByTestId("channels-credential-hint")).toHaveLength(1);
    expect(screen.getByTestId("channels-credential-hint").textContent).toContain(
      "genus channel add"
    );

    for (const input of document.querySelectorAll("input")) {
      expect(input.getAttribute("type")).not.toBe("password");
      const name = `${input.getAttribute("name") ?? ""}${input.id}${input.getAttribute("placeholder") ?? ""}`;
      expect(name.toLowerCase()).not.toMatch(/token|secret|api[_ -]?key|password/);
    }
  });

  it("offers Verify only where the bridge says it can prove something", async () => {
    mockBridge();
    await renderPage();
    expect(screen.getByTestId("channel-verify-telegram")).toBeTruthy();
    expect(screen.queryByTestId("channel-verify-pager_relay")).toBeNull();
    // The target field is part of the verify affordance, not a standing input.
    expect(screen.getByTestId("channel-verify-target-telegram")).toBeTruthy();
    expect(screen.queryByTestId("channel-verify-target-pager_relay")).toBeNull();
  });

  it("runs verify and renders each step with its own verdict", async () => {
    const bridge = mockBridge();
    await renderPage();

    fireEvent.click(screen.getByTestId("channel-verify-telegram"));
    await screen.findByTestId("channel-verify-result-telegram");

    const steps = screen.getByTestId("channel-verify-steps-telegram");
    expect(within(steps).getByTestId("channel-verify-step-telegram-auth").textContent).toContain(
      "bot reachable"
    );
    expect(within(steps).getByTestId("channel-verify-step-telegram-post").textContent).toContain(
      "chat not found"
    );
    expect(
      within(steps).getByTestId("channel-verify-step-telegram-auth").getAttribute("data-ok")
    ).toBe("true");
    expect(
      within(steps).getByTestId("channel-verify-step-telegram-post").getAttribute("data-ok")
    ).toBe("false");

    expect(callsTo(bridge.calls, "/api/channels/telegram/verify")).toHaveLength(1);
  });

  it("sends the target the operator typed, and omits it when blank", async () => {
    const bridge = mockBridge();
    await renderPage();

    fireEvent.click(screen.getByTestId("channel-verify-telegram"));
    await screen.findByTestId("channel-verify-result-telegram");
    expect(callsTo(bridge.calls, "/verify")[0].body).toEqual({});

    fireEvent.change(screen.getByTestId("channel-verify-target-telegram"), {
      target: { value: "  alice@example.com  " },
    });
    fireEvent.click(screen.getByTestId("channel-verify-telegram"));
    await waitFor(() => expect(callsTo(bridge.calls, "/verify")).toHaveLength(2));
    expect(callsTo(bridge.calls, "/verify")[1].body).toEqual({ target: "alice@example.com" });
  });

  it("never renders a channel that raised or hung as a pass", async () => {
    const bridge = mockBridge();
    bridge.reply("/verify", 200, {
      channel: "telegram",
      configured: null,
      verify_available: true,
      error_class: "TimeoutError",
      steps: [{ step: "verify", ok: true, detail: "this must not be believed" }],
    });
    await renderPage();

    fireEvent.click(screen.getByTestId("channel-verify-telegram"));
    const result = await screen.findByTestId("channel-verify-result-telegram");

    expect(result.textContent).toMatch(/could not verify/i);
    expect(result.textContent).toContain("TimeoutError");
    expect(result.getAttribute("data-verdict")).toBe("unknown");
    expect(result.textContent).not.toMatch(/verified|passed|working/i);
  });

  it("tells a never-configured channel apart from a failure", async () => {
    const bridge = mockBridge();
    bridge.reply("/verify", 200, {
      channel: "email",
      configured: false,
      verify_available: true,
      error_class: null,
      steps: [{ step: "configuration", ok: false, detail: "no credentials on this instance" }],
    });
    await renderPage();

    fireEvent.click(screen.getByTestId("channel-verify-email"));
    const result = await screen.findByTestId("channel-verify-result-email");
    expect(result.getAttribute("data-verdict")).toBe("unconfigured");
    expect(result.textContent).toMatch(/never been set up|not configured/i);
    expect(result.textContent).not.toMatch(/failed/i);
  });

  it("says so when a channel can prove nothing about itself", async () => {
    const bridge = mockBridge();
    bridge.reply("/verify", 200, {
      channel: "telegram",
      configured: true,
      verify_available: false,
      error_class: null,
      steps: [],
    });
    await renderPage();

    fireEvent.click(screen.getByTestId("channel-verify-telegram"));
    const result = await screen.findByTestId("channel-verify-result-telegram");
    expect(result.getAttribute("data-verdict")).toBe("unknown");
    expect(result.textContent).toMatch(/cannot prove|no verification/i);
  });

  it("renders the bridge's own sentence when verify is refused", async () => {
    const bridge = mockBridge();
    bridge.reply("/verify", 422, { detail: "that is not a delivery target" });
    await renderPage();

    fireEvent.click(screen.getByTestId("channel-verify-telegram"));
    expect((await screen.findByTestId("channel-verify-error-telegram")).textContent).toBe(
      "that is not a delivery target"
    );
  });

  it("reads pending pairings and identities only once the operator opens access", async () => {
    const bridge = mockBridge();
    await renderPage();
    expect(callsTo(bridge.calls, "/pending", "GET")).toHaveLength(0);

    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");

    expect(callsTo(bridge.calls, "/api/channels/telegram/pending", "GET")).toHaveLength(1);
    expect(callsTo(bridge.calls, "/api/channels/telegram/identities", "GET")).toHaveLength(1);
    expect(screen.getByTestId(`channel-pending-${PENDING.pending[0].id}`)).toBeTruthy();
    expect(screen.getByTestId(`channel-identity-${IDENTITIES.identities[0].id}`).textContent).toContain(
      "Alice"
    );
  });

  it("says that the pairing code is never returned and must come from the sender", async () => {
    mockBridge();
    await renderPage();
    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");

    expect(screen.getByTestId("channel-pending-hint-telegram").textContent).toMatch(
      /code .*never|ask the sender/i
    );
  });

  it("approves a pairing with the code the operator was given", async () => {
    const bridge = mockBridge();
    await renderPage();
    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");

    const id = PENDING.pending[0].id;
    fireEvent.click(screen.getByTestId(`pairing-settle-${id}`));
    fireEvent.change(screen.getByTestId(`pairing-code-${id}`), { target: { value: "k7m2ph" } });
    fireEvent.change(screen.getByTestId(`pairing-email-${id}`), {
      target: { value: "alice@example.com" },
    });
    fireEvent.change(screen.getByTestId(`pairing-role-${id}`), { target: { value: "member" } });
    fireEvent.click(screen.getByTestId(`pairing-approve-${id}`));

    await waitFor(() =>
      expect(callsTo(bridge.calls, "/pairings/K7M2PH/approve")).toHaveLength(1)
    );
    expect(callsTo(bridge.calls, "/pairings/K7M2PH/approve")[0].body).toEqual({
      email: "alice@example.com",
      role: "member",
    });
  });

  it("denies a pairing with the code alone", async () => {
    const bridge = mockBridge();
    await renderPage();
    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");

    const id = PENDING.pending[1].id;
    fireEvent.click(screen.getByTestId(`pairing-settle-${id}`));
    fireEvent.change(screen.getByTestId(`pairing-code-${id}`), { target: { value: "QQ23WE" } });
    fireEvent.click(screen.getByTestId(`pairing-deny-${id}`));

    await waitFor(() => expect(callsTo(bridge.calls, "/pairings/QQ23WE/deny")).toHaveLength(1));
    expect(callsTo(bridge.calls, "/pairings/QQ23WE/deny")[0].body).toBeNull();
  });

  it("renders the bridge's refusal on a pairing verbatim", async () => {
    const bridge = mockBridge();
    bridge.reply("/approve", 409, { detail: "that code has already been spent" });
    await renderPage();
    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");

    const id = PENDING.pending[0].id;
    fireEvent.click(screen.getByTestId(`pairing-settle-${id}`));
    fireEvent.change(screen.getByTestId(`pairing-code-${id}`), { target: { value: "K7M2PH" } });
    fireEvent.change(screen.getByTestId(`pairing-email-${id}`), {
      target: { value: "alice@example.com" },
    });
    fireEvent.click(screen.getByTestId(`pairing-approve-${id}`));

    expect((await screen.findByTestId(`pairing-error-${id}`)).textContent).toBe(
      "that code has already been spent"
    );
  });

  it("removes a binding only after an inline confirmation", async () => {
    const bridge = mockBridge();
    await renderPage();
    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");

    const id = IDENTITIES.identities[0].id;
    fireEvent.click(screen.getByTestId(`identity-remove-${id}`));
    expect(callsTo(bridge.calls, "/identities/", "DELETE")).toHaveLength(0);

    fireEvent.click(screen.getByTestId(`identity-remove-confirm-${id}`));
    await waitFor(() =>
      expect(
        callsTo(bridge.calls, `/api/channels/telegram/identities/${id}`, "DELETE")
      ).toHaveLength(1)
    );
  });

  it("uses no browser dialog for a destructive action", async () => {
    mockBridge();
    const confirmSpy = vi.fn(() => true);
    vi.stubGlobal("confirm", confirmSpy);
    await renderPage();
    fireEvent.click(screen.getByTestId("channel-access-toggle-telegram"));
    await screen.findByTestId("channel-access-telegram");
    fireEvent.click(screen.getByTestId(`identity-remove-${IDENTITIES.identities[0].id}`));
    fireEvent.click(screen.getByTestId(`identity-remove-confirm-${IDENTITIES.identities[0].id}`));
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("renders the bridge's own words when the listing fails", async () => {
    const bridge = mockBridge();
    bridge.reply("/api/channels", 502, { detail: "could not read the channel state" });
    render(<ChannelsPage visible />);
    expect((await screen.findByTestId("channels-error")).textContent).toContain(
      "could not read the channel state"
    );
  });

  it("says the engine carries no channels rather than showing an empty page", async () => {
    mockBridge({ channels: [] });
    render(<ChannelsPage visible />);
    await screen.findByTestId("channels-empty");
  });
});

describe("ChannelsPage polling", () => {
  it("does not touch the bridge while the page is hidden", () => {
    const { fetchMock } = mockBridge();
    render(<ChannelsPage visible={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-reads the pending counts every 60 seconds while visible", async () => {
    const bridge = mockBridge();
    await renderPage();
    expect(callsTo(bridge.calls, "/api/channels", "GET")).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(60_000);
    await waitFor(() => expect(callsTo(bridge.calls, "/api/channels", "GET")).toHaveLength(2));
  });

  it("stops polling once the page is hidden", async () => {
    const bridge = mockBridge();
    const { rerender } = render(<ChannelsPage visible />);
    await screen.findByTestId("channel-card-telegram");

    rerender(<ChannelsPage visible={false} />);
    const before = callsTo(bridge.calls, "/api/channels", "GET").length;
    await vi.advanceTimersByTimeAsync(180_000);
    expect(callsTo(bridge.calls, "/api/channels", "GET")).toHaveLength(before);
  });

  it("Refresh re-reads on demand", async () => {
    const bridge = mockBridge();
    await renderPage();
    fireEvent.click(screen.getByTestId("channels-refresh"));
    await waitFor(() => expect(callsTo(bridge.calls, "/api/channels", "GET")).toHaveLength(2));
  });
});
