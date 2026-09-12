/**
 * The first-run wizard, and the redirect that sends a fresh install to it.
 *
 * What these hold down is the behaviour a reviewer would otherwise have to
 * take on trust:
 *
 * - the printed token is exchanged once and then removed from the address bar,
 *   and the claim it buys is never written to browser storage;
 * - the provider step cannot be passed with a key that did not work — the
 *   "configured but not working" state is the one the whole wizard exists to
 *   prevent;
 * - the channel step can be skipped, because a channel-free instance is a
 *   supported deployment;
 * - the owner's second factor is a step, not a suggestion;
 * - `proxy.ts` redirects to `/setup` only while the bridge says setup is
 *   incomplete, and 404s the page once it is not.
 *
 * `fireEvent` rather than `user-event`: the latter is not a dependency of this
 * app, and every interaction here is a change or a click, which `fireEvent`
 * models exactly.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type SignInResult = { ok: boolean; error?: string };
type SignIn = (
  provider: string,
  options?: Record<string, unknown>,
) => Promise<SignInResult>;
const signIn = vi.fn<SignIn>(async () => ({ ok: true }));
vi.mock("next-auth/react", () => ({ signIn }));
vi.mock("@/components/account-security-panel", () => ({
  AccountSecurityPanel: () => (
    <div data-testid="mfa-panel">enrol a second factor</div>
  ),
}));
const auth = vi.fn((handler: unknown) => handler);
vi.mock("@/lib/auth", () => ({ auth }));

const { SetupWizard } = await import("@/app/setup/setup-wizard");
const { setupRedirect, setupIncomplete, resetSetupStatusCache } =
  await import("@/proxy");

const CLAIM = "claim-token-value";
const FIXTURE_KEY = "sk-test-1111111111111111111111111";
const FIXTURE_PASSWORD = "correct-horse-battery-staple";
const MODEL = "openrouter/openai/gpt-5.4";

const DETECTED = {
  providers: [{ id: "openrouter", label: "OpenRouter", configured: false }],
  models: [{ id: MODEL, provider: "openrouter" }],
  ollama: { reachable: false, tool_models: [] },
  telegram: { configured: false },
  doctor: {
    checks: [
      { id: "db.connect", status: "pass" },
      { id: "provider.keys", status: "fail" },
    ],
  },
};

type Handler = (body: unknown) => { status?: number; json: unknown };
type Call = { path: string; body: unknown; authorization: string | null };

function stubFetch(handlers: Record<string, Handler>): Call[] {
  const calls: Call[] = [];
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ path, body, authorization: headers.Authorization ?? null });
      const handler = handlers[path];
      const result = handler ? handler(body) : { status: 404, json: {} };
      const status = result.status ?? 200;
      return {
        ok: status < 400,
        status,
        json: async () => result.json,
      } as Response;
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const happyPath: Record<string, Handler> = {
  "/api/setup/claim": () => ({ json: { claim_token: CLAIM, expires_in: 300 } }),
  "/api/setup/detect": () => ({ json: DETECTED }),
  "/api/setup/operator": () => ({
    json: {
      user: { role: "owner" },
      mfa_setup_required: true,
      signed_in: true,
    },
  }),
  "/api/setup/provider": () => ({
    json: { ok: true, latency_ms: 42, error_class: null },
  }),
  "/api/setup/channel": () => ({ json: { ok: true, bot: "genus_test_bot" } }),
  "/api/setup/agent": () => ({ json: { installed: ["main", "concierge"] } }),
  "/api/setup/complete": () => ({ json: { next: "/?v=chat" } }),
};

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function click(name: string) {
  fireEvent.click(screen.getByRole("button", { name }));
}

async function start() {
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Start" })).toBeEnabled(),
  );
  click("Start");
}

async function createOperator() {
  fill("Full name", "Alice Example");
  fill("Email", "alice@example.com");
  fill("Password", FIXTURE_PASSWORD);
  fill("Confirm password", FIXTURE_PASSWORD);
  click("Create account");
  await waitFor(() =>
    expect(screen.getByLabelText("Provider")).toBeInTheDocument(),
  );
}

/** Drive the provider step to a passing test and stop there. */
async function passProvider() {
  fill("Provider", "openrouter");
  fill("API key", FIXTURE_KEY);
  fill("Default model", MODEL);
  click("Test connection");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled(),
  );
}

/** Everything up to and including the agents install. */
async function reachTwoFactorStep() {
  await start();
  await createOperator();
  await passProvider();
  click("Next");
  click("Skip");
  click("Install");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Finish setup" })).toBeEnabled(),
  );
}

describe("the first-run wizard", () => {
  beforeEach(() => {
    signIn.mockClear();
    signIn.mockResolvedValue({ ok: true });
    window.history.replaceState({}, "", "/setup?token=printed-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
    sessionStorage.clear();
  });

  it("exchanges the printed token once and clears it from the address bar", async () => {
    const calls = stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await waitFor(() =>
      expect(calls.filter((c) => c.path === "/api/setup/claim")).toHaveLength(
        1,
      ),
    );
    expect(calls[0].body).toEqual({ token: "printed-token" });
    await waitFor(() => expect(window.location.search).toBe(""));
  });

  it("keeps the claim token out of browser storage", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Start" })).toBeEnabled(),
    );
    expect(JSON.stringify(localStorage)).not.toContain(CLAIM);
    expect(JSON.stringify(sessionStorage)).not.toContain(CLAIM);
  });

  it("carries the claim as a bearer on a step call", async () => {
    const calls = stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();

    expect(
      calls.find((c) => c.path === "/api/setup/operator")?.authorization,
    ).toBe(`Bearer ${CLAIM}`);
    // The claim route itself is the one call with no bearer: it IS the exchange.
    expect(
      calls.find((c) => c.path === "/api/setup/claim")?.authorization,
    ).toBeNull();
  });

  it("says so, and offers nothing, when the link is spent", async () => {
    stubFetch({
      ...happyPath,
      "/api/setup/claim": () => ({ status: 401, json: {} }),
    });
    render(<SetupWizard initialToken="printed-token" />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toContain(
      "genus auth setup-link",
    );
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("refuses to create an account when the two passwords differ", async () => {
    const calls = stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    fill("Full name", "Alice Example");
    fill("Email", "alice@example.com");
    fill("Password", FIXTURE_PASSWORD);
    fill("Confirm password", "something-else-entirely");
    click("Create account");

    expect(screen.getByRole("alert").textContent).toContain("do not match");
    expect(calls.some((c) => c.path === "/api/setup/operator")).toBe(false);
  });

  it("refuses a password under the same minimum the bridge enforces", async () => {
    const calls = stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    fill("Full name", "Alice Example");
    fill("Email", "alice@example.com");
    fill("Password", "short");
    fill("Confirm password", "short");
    click("Create account");

    expect(screen.getByRole("alert").textContent).toContain("12 characters");
    expect(calls.some((c) => c.path === "/api/setup/operator")).toBe(false);
  });

  it("hands off to a real sign-in once the account exists", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();

    await waitFor(() => expect(signIn).toHaveBeenCalledTimes(1));
    expect(signIn.mock.calls[0][0]).toBe("local");
  });

  it("cannot advance past the provider step without a passing test", async () => {
    stubFetch({
      ...happyPath,
      "/api/setup/provider": () => ({
        json: { ok: false, latency_ms: 0, error_class: "AuthenticationError" },
      }),
    });
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();

    // Next is dead before a test has been run at all.
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    fill("Provider", "openrouter");
    fill("API key", FIXTURE_KEY);
    fill("Default model", MODEL);
    click("Test connection");

    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain("did not work"),
    );
    expect(screen.getByRole("status").textContent).toContain(
      "AuthenticationError",
    );
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("advances past the provider step when the key really works", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();
    await passProvider();
    click("Next");

    expect(screen.getByLabelText("Telegram bot token")).toBeInTheDocument();
  });

  it("clears the key from state once it has been stored", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();
    await passProvider();

    expect((screen.getByLabelText("API key") as HTMLInputElement).value).toBe(
      "",
    );
  });

  it("re-arms the gate when the key is edited after a passing test", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();
    await passProvider();
    fill("API key", "a-different-key");

    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("lets the channel step be skipped", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();
    await passProvider();
    click("Next");
    click("Skip");

    expect(screen.getByRole("button", { name: /Minimal/ })).toBeInTheDocument();
  });

  it("preselects the standard agent preset", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();
    await passProvider();
    click("Next");
    click("Skip");

    expect(screen.getByRole("button", { name: /Standard/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /Minimal/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("puts the owner's second factor in the flow, not in a banner", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await reachTwoFactorStep();

    expect(screen.getByTestId("mfa-panel")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2 }).textContent).toContain(
      "Two-factor",
    );
  });

  it("closes setup and points at the chat", async () => {
    const calls = stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await reachTwoFactorStep();
    click("Finish setup");

    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "Open the dashboard" }),
      ).toHaveAttribute("href", "/?v=chat"),
    );
    expect(calls.some((c) => c.path === "/api/setup/complete")).toBe(true);
  });

  it("sends the operator to sign-in when the browser hand-off could not happen", async () => {
    signIn.mockResolvedValue({ ok: false, error: "CredentialsSignin" });
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await reachTwoFactorStep();

    // No session, so no enrolment panel — and an honest instruction instead.
    expect(screen.queryByTestId("mfa-panel")).not.toBeInTheDocument();
    expect(screen.getByRole("alert").textContent).toContain(
      "could not be signed in automatically",
    );

    click("Finish setup");
    await waitFor(() =>
      expect(
        screen.getByRole("link", { name: "Go to sign in" }),
      ).toHaveAttribute("href", "/signin"),
    );
  });

  it("shows the doctor's required checks as pills from the provider step on", async () => {
    stubFetch(happyPath);
    render(<SetupWizard initialToken="printed-token" />);

    await start();
    expect(screen.queryByLabelText("Instance checks")).not.toBeInTheDocument();

    await createOperator();

    expect(screen.getByLabelText("Instance checks")).toBeInTheDocument();
    expect(screen.getByText("db.connect")).toBeInTheDocument();
    expect(screen.getByText("provider.keys")).toBeInTheDocument();
  });

  it("never renders a credential it was given back to the operator", async () => {
    stubFetch(happyPath);
    const { container } = render(<SetupWizard initialToken="printed-token" />);

    await start();
    await createOperator();
    await passProvider();

    expect(container.textContent).not.toContain(FIXTURE_KEY);
    expect(container.textContent).not.toContain(FIXTURE_PASSWORD);
    expect(container.textContent).not.toContain("printed-token");
  });
});

// ── the redirect ─────────────────────────────────────────────────────

function request(path: string) {
  return new NextRequest(`https://genus.example${path}`);
}

describe("the first-run redirect in proxy.ts", () => {
  afterEach(() => {
    resetSetupStatusCache();
    vi.unstubAllGlobals();
  });

  it("sends a dashboard page to /setup while setup is incomplete", () => {
    const response = setupRedirect(request("/tasks?view=mine"), true);

    expect(response?.status).toBe(307);
    expect(new URL(response!.headers.get("location")!).pathname).toBe("/setup");
  });

  it("leaves /setup and the setup API alone, or the redirect chases itself", () => {
    expect(setupRedirect(request("/setup?token=x"), true)).toBeNull();
    expect(setupRedirect(request("/api/setup/claim"), true)).toBeNull();
  });

  it("never answers an API request with a page", () => {
    expect(setupRedirect(request("/api/bridge/people"), true)).toBeNull();
  });

  it("does nothing at all once setup is complete", () => {
    expect(setupRedirect(request("/tasks"), false)).toBeNull();
    expect(setupRedirect(request("/api/bridge/people"), false)).toBeNull();
  });

  it("404s the wizard once setup is complete", () => {
    expect(setupRedirect(request("/setup"), false)?.status).toBe(404);
    expect(setupRedirect(request("/setup/anything"), false)?.status).toBe(404);
  });

  it("reads the bridge's 200 as incomplete", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ complete: false }),
      })),
    );

    await expect(setupIncomplete("http://bridge.test")).resolves.toBe(true);
  });

  it("reads the bridge's 404 as complete — the router is gone by then", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 404, json: async () => ({}) })),
    );

    await expect(setupIncomplete("http://bridge.test")).resolves.toBe(false);
  });

  it("reads an unreachable bridge as complete rather than redirecting everyone", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );

    await expect(setupIncomplete("http://bridge.test")).resolves.toBe(false);
  });

  it("asks the bridge at most once per window", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ complete: false }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    await setupIncomplete("http://bridge.test", 1_000);
    await setupIncomplete("http://bridge.test", 2_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await setupIncomplete("http://bridge.test", 20_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
