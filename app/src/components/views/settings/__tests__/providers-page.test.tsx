/**
 * The Providers page is the only screen in the Helm an operator hands a
 * credential to, so the tests that matter most are the ones about what does
 * NOT happen: the typed key never survives in the DOM, never reaches a URL,
 * and nothing the bridge answers with is ever a key.
 *
 * Every fetch is mocked against the exact shapes the bridge returns
 * (crm/bridge/routers/providers.py and robothor/engine/admin_providers.py) —
 * inventing a field here would let the page ship against an API that does not
 * exist.
 */
import { render, screen, fireEvent, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProvidersPage } from "../providers-page";

const PROVIDERS = {
  providers: [
    {
      id: "openrouter",
      label: "OpenRouter",
      configured: true,
      env_var: "OPENROUTER_API_KEY",
      default_model: "openrouter/openai/gpt-5.4",
      slots: [
        {
          position: 1,
          source: "vault",
          fingerprint: "sha256:11aa22bb",
          state: "active",
          updated_at: "2026-09-01T10:00:00+00:00",
        },
        {
          position: 2,
          source: "vault",
          fingerprint: "sha256:33cc44dd",
          state: "spare",
          updated_at: null,
        },
      ],
    },
    {
      id: "anthropic",
      label: "Anthropic",
      configured: true,
      env_var: "ANTHROPIC_API_KEY",
      default_model: "anthropic/claude-sonnet-4.6",
      slots: [
        {
          position: 1,
          source: "env",
          fingerprint: "sha256:55ee66ff",
          state: "active",
          updated_at: null,
        },
      ],
    },
    {
      id: "openai",
      label: "OpenAI",
      configured: false,
      env_var: "OPENAI_API_KEY",
      default_model: "openai/gpt-5.4",
      slots: [],
    },
  ],
};

const MODELS = {
  models: [
    {
      id: "openrouter/openai/gpt-5.4",
      provider: "openrouter",
      context_window: 400000,
      supports_thinking: true,
      supports_tools: true,
      source: "registry",
    },
    {
      id: "anthropic/claude-sonnet-4.6",
      provider: "anthropic",
      context_window: 200000,
      supports_thinking: true,
      supports_tools: true,
      source: "registry",
    },
    {
      id: "ollama_chat/qwen3:8b",
      provider: "ollama_chat",
      context_window: null,
      supports_thinking: false,
      supports_tools: null,
      source: "plugin",
    },
  ],
};

interface Reply {
  status?: number;
  body: unknown;
}

type Route = Reply | ((body: unknown) => Reply);

interface Call {
  url: string;
  method: string;
  body: Record<string, unknown> | undefined;
}

const calls: Call[] = [];

function installFetch(routes: Record<string, Route> = {}) {
  const table: Record<string, Route> = {
    "GET /api/bridge/api/providers": { body: PROVIDERS },
    "GET /api/bridge/api/models": { body: MODELS },
    "GET /api/bridge/api/providers/defaults": { body: { primary: null, fallbacks: [] } },
    ...routes,
  };
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
    calls.push({ url, method, body });
    const route = table[`${method} ${url}`];
    if (!route) {
      return { ok: false, status: 404, json: async () => ({ detail: `no route for ${method} ${url}` }) } as Response;
    }
    const reply = typeof route === "function" ? route(body) : route;
    const status = reply.status ?? 200;
    return {
      ok: status < 400,
      status,
      json: async () => reply.body,
    } as Response;
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  calls.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderPage(routes: Record<string, Route> = {}) {
  const mock = installFetch(routes);
  render(<ProvidersPage />);
  await screen.findByTestId("provider-row-openrouter");
  return mock;
}

describe("ProvidersPage — the listing", () => {
  it("says it is loading before the bridge answers", () => {
    installFetch();
    render(<ProvidersPage />);
    expect(screen.getByTestId("providers-loading")).toBeInTheDocument();
  });

  it("renders one row per provider with its fingerprint and state", async () => {
    await renderPage();
    for (const id of ["openrouter", "anthropic", "openai"]) {
      expect(screen.getByTestId(`provider-row-${id}`)).toBeInTheDocument();
    }
    const row = screen.getByTestId("provider-row-openrouter");
    expect(row).toHaveTextContent("OpenRouter");
    expect(row).toHaveTextContent("sha256:11aa22bb");
    expect(row).toHaveTextContent("sha256:33cc44dd");
    expect(within(row).getByTestId("provider-slot-openrouter-1")).toHaveTextContent(/active/i);
    expect(within(row).getByTestId("provider-slot-openrouter-2")).toHaveTextContent(/spare/i);
  });

  it("marks a provider with no key as not set", async () => {
    await renderPage();
    const row = screen.getByTestId("provider-row-openai");
    expect(within(row).getByTestId("provider-key-state-openai")).toHaveTextContent(/not set/i);
  });

  it("marks an env-sourced key read-only and refuses to remove it", async () => {
    await renderPage();
    const row = screen.getByTestId("provider-row-anthropic");
    expect(row).toHaveTextContent(/read-only/i);
    const remove = within(row).getByTestId("provider-remove-anthropic-1");
    expect(remove).toBeDisabled();
    // The reason has to be legible, not a bare disabled button.
    expect(within(row).getByTestId("provider-remove-reason-anthropic-1")).toHaveTextContent(
      /ANTHROPIC_API_KEY/
    );
  });

  it("says nothing is configured when the appliance knows no providers", async () => {
    installFetch({ "GET /api/bridge/api/providers": { body: { providers: [] } } });
    render(<ProvidersPage />);
    expect(await screen.findByTestId("providers-empty")).toBeInTheDocument();
  });

  it("renders the server's own message when the listing fails", async () => {
    installFetch({
      "GET /api/bridge/api/providers": { status: 502, body: { detail: "could not read the provider state" } },
    });
    render(<ProvidersPage />);
    const error = await screen.findByTestId("providers-error");
    expect(error).toHaveTextContent("could not read the provider state");
  });

  it("renders nothing from a listing field that carries key material", async () => {
    // The bridge never sends this. If a future one did — or a compromised hop
    // added it — the page must render the fields it knows and nothing else.
    // Asserting against a fixture that contains no key at all would be a test
    // that cannot fail.
    const leaky = {
      providers: [
        { ...PROVIDERS.providers[2], api_key: "sk-live-should-never-render", value: "sk-live-2" },
      ],
    };
    installFetch({ "GET /api/bridge/api/providers": { body: leaky } });
    render(<ProvidersPage />);
    await screen.findByTestId("provider-row-openai");
    expect(document.body.innerHTML).not.toMatch(/sk-live/);
  });

  it("survives a provider payload with no slots at all", async () => {
    // A dashboard talking to an older or newer engine gets a shape it did not
    // expect. The table may be wrong; the Helm may not go blank.
    const partial = {
      providers: [
        {
          id: "openrouter",
          label: "OpenRouter",
          configured: true,
          env_var: "OPENROUTER_API_KEY",
          default_model: "openrouter/openai/gpt-5.4",
        },
      ],
    };
    installFetch({ "GET /api/bridge/api/providers": { body: partial } });
    render(<ProvidersPage />);
    const row = await screen.findByTestId("provider-row-openrouter");
    expect(row).toHaveTextContent("OpenRouter");
    expect(within(row).getByTestId("provider-key-state-openrouter")).toHaveTextContent(/not set/i);
  });

  it("reads the key state from the slots, not from a flag that can disagree", async () => {
    const inconsistent = {
      providers: [{ ...PROVIDERS.providers[2], configured: true, slots: [] }],
    };
    installFetch({ "GET /api/bridge/api/providers": { body: inconsistent } });
    render(<ProvidersPage />);
    await screen.findByTestId("provider-row-openai");
    expect(screen.getByTestId("provider-key-state-openai")).toHaveTextContent(/not set/i);
  });

  it("keeps the table it already has when a refresh fails", async () => {
    let attempt = 0;
    installFetch({
      "GET /api/bridge/api/providers": () => {
        attempt += 1;
        return attempt === 1
          ? { body: PROVIDERS }
          : { status: 502, body: { detail: "the bridge is unavailable" } };
      },
    });
    render(<ProvidersPage />);
    await screen.findByTestId("provider-row-openai");
    fireEvent.click(screen.getByTestId("providers-refresh"));
    await screen.findByTestId("providers-error");
    // The rows are stale, not gone: an operator reading a digest must not lose
    // it because a refresh bounced.
    expect(screen.getByTestId("provider-row-openai")).toBeInTheDocument();
  });
});

describe("ProvidersPage — test connection", () => {
  it("reports a good result with its latency", async () => {
    await renderPage({
      "POST /api/bridge/api/providers/openrouter/test": {
        body: {
          ok: true,
          model: "openrouter/openai/gpt-5.4",
          latency_ms: 412,
          error_class: null,
          message: "OpenRouter answered in 412ms.",
        },
      },
    });
    fireEvent.click(screen.getByTestId("provider-test-openrouter"));
    const result = await screen.findByTestId("provider-test-result-openrouter");
    expect(result).toHaveTextContent(/412/);
    expect(result).toHaveAttribute("aria-live", "polite");
    // No candidate key on a row-level test: the stored credential is the subject.
    const testCall = calls.find((c) => c.method === "POST");
    expect(testCall?.body).toEqual({});
  });

  it("reports the error class and the provider's message on a failure", async () => {
    await renderPage({
      "POST /api/bridge/api/providers/openrouter/test": {
        body: {
          ok: false,
          model: "openrouter/openai/gpt-5.4",
          latency_ms: 88,
          error_class: "auth",
          message: "AuthenticationError: no auth credentials found",
        },
      },
    });
    fireEvent.click(screen.getByTestId("provider-test-openrouter"));
    const result = await screen.findByTestId("provider-test-result-openrouter");
    expect(result).toHaveTextContent(/auth/i);
    expect(result).toHaveTextContent(/no auth credentials found/i);
  });

  it("says a provider has not been tested in this session until it is", async () => {
    await renderPage();
    expect(screen.getByTestId("provider-test-result-openai")).toHaveTextContent(/not tested/i);
  });
});

describe("ProvidersPage — add or rotate a key", () => {
  it("takes the key in a password field and never puts it in a URL", async () => {
    await renderPage({
      "PUT /api/bridge/api/providers/openai/keys/1": {
        body: { configured: true, fingerprint: "sha256:99887766", position: 1 },
      },
    });
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    const input = screen.getByTestId("provider-key-input-openai") as HTMLInputElement;
    expect(input.type).toBe("password");

    fireEvent.change(input, { target: { value: "sk-live-secret-value" } });
    fireEvent.click(screen.getByTestId("provider-key-save-openai"));

    const put = await vi.waitFor(() => {
      const call = calls.find((c) => c.method === "PUT");
      expect(call).toBeDefined();
      return call!;
    });
    expect(put.url).toBe("/api/bridge/api/providers/openai/keys/1");
    expect(put.body).toEqual({ api_key: "sk-live-secret-value" });
    for (const call of calls) expect(call.url).not.toContain("sk-live-secret-value");
  });

  it("tests a candidate key before it is stored and shows the verdict inline", async () => {
    await renderPage({
      "POST /api/bridge/api/providers/openai/test": {
        body: { ok: true, model: "openai/gpt-5.4", latency_ms: 310, error_class: null, message: "OpenAI answered in 310ms." },
      },
    });
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    fireEvent.change(screen.getByTestId("provider-key-input-openai"), {
      target: { value: "sk-candidate" },
    });
    fireEvent.click(screen.getByTestId("provider-key-test-openai"));

    const verdict = await screen.findByTestId("provider-key-test-result-openai");
    expect(verdict).toHaveAttribute("aria-live", "polite");
    expect(verdict).toHaveTextContent(/310/);
    const call = calls.find((c) => c.method === "POST");
    expect(call?.body).toEqual({ api_key: "sk-candidate" });
    // Nothing was stored by a test.
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("clears the field on save and leaves the value nowhere in the document", async () => {
    await renderPage({
      "PUT /api/bridge/api/providers/openai/keys/1": {
        body: { configured: true, fingerprint: "sha256:99887766", position: 1 },
      },
    });
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    fireEvent.change(screen.getByTestId("provider-key-input-openai"), {
      target: { value: "sk-live-secret-value" },
    });
    fireEvent.click(screen.getByTestId("provider-key-save-openai"));

    await vi.waitFor(() => {
      expect(screen.queryByTestId("provider-key-input-openai")).toBeNull();
    });
    expect(document.body.innerHTML).not.toContain("sk-live-secret-value");
  });

  it("clears the field on cancel too", async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    fireEvent.change(screen.getByTestId("provider-key-input-openai"), {
      target: { value: "sk-live-secret-value" },
    });
    fireEvent.click(screen.getByTestId("provider-key-cancel-openai"));
    expect(screen.queryByTestId("provider-key-input-openai")).toBeNull();
    expect(document.body.innerHTML).not.toContain("sk-live-secret-value");

    // Reopening must not restore it.
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    expect((screen.getByTestId("provider-key-input-openai") as HTMLInputElement).value).toBe("");
  });

  it("renders the server's refusal, not a generic failure", async () => {
    await renderPage({
      "PUT /api/bridge/api/providers/openrouter/keys/3": {
        status: 409,
        body: { detail: "slot 3 is empty, so a key in slot 3 would never be used — fill slot 3 first" },
      },
    });
    fireEvent.click(screen.getByTestId("provider-add-openrouter"));
    fireEvent.change(screen.getByTestId("provider-key-input-openrouter"), {
      target: { value: "sk-live-secret-value" },
    });
    fireEvent.change(screen.getByTestId("provider-key-slot-openrouter"), { target: { value: "3" } });
    fireEvent.click(screen.getByTestId("provider-key-save-openrouter"));

    const error = await screen.findByTestId("provider-key-error-openrouter");
    expect(error).toHaveTextContent(/fill slot 3 first/);
  });

  it("drops a stale refusal as soon as the key is retyped", async () => {
    await renderPage({
      "PUT /api/bridge/api/providers/openai/keys/1": {
        status: 503,
        body: { detail: "the vault is down" },
      },
    });
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    fireEvent.change(screen.getByTestId("provider-key-input-openai"), {
      target: { value: "sk-first" },
    });
    fireEvent.click(screen.getByTestId("provider-key-save-openai"));
    await screen.findByTestId("provider-key-error-openai");

    fireEvent.change(screen.getByTestId("provider-key-input-openai"), {
      target: { value: "sk-second" },
    });
    expect(screen.queryByTestId("provider-key-error-openai")).toBeNull();
  });

  it("scrubs the typed key out of a server message that echoes it", async () => {
    // Defence in depth: no bridge route produces such a message today, and the
    // page holds the only copy of the value, so it can always take it back out.
    await renderPage({
      "PUT /api/bridge/api/providers/openai/keys/1": {
        status: 500,
        body: { detail: "vault write failed for sk-live-echoed-back" },
      },
    });
    fireEvent.click(screen.getByTestId("provider-add-openai"));
    fireEvent.change(screen.getByTestId("provider-key-input-openai"), {
      target: { value: "sk-live-echoed-back" },
    });
    fireEvent.click(screen.getByTestId("provider-key-save-openai"));

    const error = await screen.findByTestId("provider-key-error-openai");
    expect(error).toHaveTextContent(/vault write failed/);
    expect(error.textContent).not.toContain("sk-live-echoed-back");
  });

  it("disarms a pending removal when the key form is opened", async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId("provider-remove-openrouter-1"));
    expect(screen.getByTestId("provider-remove-confirm-openrouter-1")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("provider-add-openrouter"));
    expect(screen.queryByTestId("provider-remove-confirm-openrouter-1")).toBeNull();
  });

  it("offers each occupied slot to rotate and one free slot to add", async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId("provider-add-openrouter"));
    const slot = screen.getByTestId("provider-key-slot-openrouter") as HTMLSelectElement;
    const values = Array.from(slot.options).map((option) => option.value);
    expect(values).toEqual(["1", "2", "3"]);
  });
});

describe("ProvidersPage — remove a key", () => {
  it("asks inline, never with a browser dialog, and deletes on confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    await renderPage({
      "DELETE /api/bridge/api/providers/openrouter/keys/2": {
        body: { configured: false, position: 2, removed: true },
      },
    });
    fireEvent.click(screen.getByTestId("provider-remove-openrouter-2"));
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId("provider-remove-confirm-openrouter-2")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("provider-remove-confirm-openrouter-2"));
    const call = await vi.waitFor(() => {
      const found = calls.find((c) => c.method === "DELETE");
      expect(found).toBeDefined();
      return found!;
    });
    expect(call.url).toBe("/api/bridge/api/providers/openrouter/keys/2");
  });

  it("keeps the key when the confirmation is dismissed", async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId("provider-remove-openrouter-2"));
    fireEvent.click(screen.getByTestId("provider-remove-cancel-openrouter-2"));
    expect(screen.queryByTestId("provider-remove-confirm-openrouter-2")).toBeNull();
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
  });

  it("renders the server's refusal when a removal would strand a spare", async () => {
    await renderPage({
      "DELETE /api/bridge/api/providers/openrouter/keys/1": {
        status: 409,
        body: { detail: "slot 2 still holds a key, so removing slot 1 would strand it" },
      },
    });
    fireEvent.click(screen.getByTestId("provider-remove-openrouter-1"));
    fireEvent.click(screen.getByTestId("provider-remove-confirm-openrouter-1"));
    expect(await screen.findByTestId("provider-error-openrouter")).toHaveTextContent(
      /would strand it/
    );
  });
});

describe("ProvidersPage — the fleet default model", () => {
  it("shows the model the fleet is running on, and its fallbacks", async () => {
    await renderPage({
      "GET /api/bridge/api/providers/defaults": {
        body: {
          primary: "openrouter/openai/gpt-5.4",
          fallbacks: ["anthropic/claude-sonnet-4.6"],
        },
      },
    });
    await vi.waitFor(() => {
      expect((screen.getByTestId("fleet-default-model") as HTMLSelectElement).value).toBe(
        "openrouter/openai/gpt-5.4"
      );
    });
    expect((screen.getByTestId("fleet-fallback-0") as HTMLSelectElement).value).toBe(
      "anthropic/claude-sonnet-4.6"
    );
    expect(screen.getByTestId("fleet-default-status")).toHaveTextContent(
      /openrouter\/openai\/gpt-5\.4/
    );
  });

  it("says plainly when the instance has no default set", async () => {
    await renderPage({
      "GET /api/bridge/api/providers/defaults": { body: { primary: null, fallbacks: [] } },
    });
    expect(screen.getByTestId("fleet-default-status")).toHaveTextContent(/no default/i);
    expect((screen.getByTestId("fleet-default-model") as HTMLSelectElement).value).toBe("");
  });

  it("does not pretend to know the default when the read fails", async () => {
    await renderPage({
      "GET /api/bridge/api/providers/defaults": {
        status: 404,
        body: { detail: "Not found" },
      },
    });
    const status = await screen.findByTestId("fleet-default-status");
    expect(status).toHaveTextContent(/could not|unknown|not report/i);
    expect(status.textContent).not.toMatch(/no default is set/i);
  });

  it("keeps a default the catalog does not list, and says so", async () => {
    // Several providers' default models are not registry entries, so this is
    // the state a real instance is in. A select that silently shows "Choose a
    // model…" while the status line names the model would be lying about what
    // the fleet runs.
    await renderPage({
      "GET /api/bridge/api/providers/defaults": {
        body: { primary: "openrouter/x-ai/grok-4", fallbacks: [] },
      },
    });
    const select = screen.getByTestId("fleet-default-model") as HTMLSelectElement;
    await vi.waitFor(() => expect(select.value).toBe("openrouter/x-ai/grok-4"));
    const option = within(select).getByRole("option", { name: /grok-4/ });
    expect(option.textContent).toMatch(/not in the catalog/i);

    // And Save must not fire a PATCH the bridge will refuse with a 422 the
    // operator cannot act on from this screen.
    expect(screen.getByTestId("fleet-default-save")).toBeDisabled();
    expect(screen.getByTestId("fleet-default-blocked")).toHaveTextContent(
      /openrouter\/x-ai\/grok-4/
    );
  });

  it("re-enables Save as soon as a catalog model is chosen", async () => {
    await renderPage({
      "GET /api/bridge/api/providers/defaults": {
        body: { primary: "openrouter/x-ai/grok-4", fallbacks: [] },
      },
      "PATCH /api/bridge/api/providers/defaults": {
        body: { model: "anthropic/claude-sonnet-4.6", fallbacks: [], applied: true },
      },
    });
    await vi.waitFor(() =>
      expect(screen.getByTestId("fleet-default-save")).toBeDisabled()
    );

    fireEvent.change(screen.getByTestId("fleet-default-model"), {
      target: { value: "anthropic/claude-sonnet-4.6" },
    });
    expect(screen.getByTestId("fleet-default-save")).toBeEnabled();
    expect(screen.queryByTestId("fleet-default-blocked")).toBeNull();

    fireEvent.click(screen.getByTestId("fleet-default-save"));
    const call = await vi.waitFor(() => {
      const found = calls.find((c) => c.method === "PATCH");
      expect(found).toBeDefined();
      return found!;
    });
    expect(call.body).toEqual({ model: "anthropic/claude-sonnet-4.6", fallbacks: [] });
  });

  it("keeps an off-catalog fallback visible and blocks the save too", async () => {
    await renderPage({
      "GET /api/bridge/api/providers/defaults": {
        body: {
          primary: "anthropic/claude-sonnet-4.6",
          fallbacks: ["openrouter/x-ai/grok-4"],
        },
      },
    });
    const fallback = (await screen.findByTestId("fleet-fallback-0")) as HTMLSelectElement;
    await vi.waitFor(() => expect(fallback.value).toBe("openrouter/x-ai/grok-4"));
    expect(within(fallback).getByRole("option", { name: /grok-4/ }).textContent).toMatch(
      /not in the catalog/i
    );
    expect(screen.getByTestId("fleet-default-save")).toBeDisabled();
  });

  it("says so when the engine knows no models at all", async () => {
    await renderPage({ "GET /api/bridge/api/models": { body: { models: [] } } });
    expect(screen.getByTestId("fleet-defaults")).toHaveTextContent(/no models/i);
  });

  it("groups the model select by provider and shows the context limit", async () => {
    await renderPage();
    const select = screen.getByTestId("fleet-default-model") as HTMLSelectElement;
    const groups = Array.from(select.querySelectorAll("optgroup")).map((g) => g.label);
    expect(groups).toContain("OpenRouter");
    expect(groups).toContain("Anthropic");
    // A provider with no credential entry of its own still has to be reachable.
    expect(groups).toContain("ollama_chat");
    const option = within(select).getByRole("option", { name: /gpt-5\.4/ });
    expect(option.textContent).toMatch(/400,000|400000|400K/i);
  });

  it("writes the chosen primary and fallbacks with PATCH", async () => {
    await renderPage({
      "PATCH /api/bridge/api/providers/defaults": {
        body: {
          model: "anthropic/claude-sonnet-4.6",
          fallbacks: ["openrouter/openai/gpt-5.4"],
          applied: true,
        },
      },
    });
    fireEvent.change(screen.getByTestId("fleet-default-model"), {
      target: { value: "anthropic/claude-sonnet-4.6" },
    });
    fireEvent.click(screen.getByTestId("fleet-fallback-add"));
    fireEvent.change(screen.getByTestId("fleet-fallback-0"), {
      target: { value: "openrouter/openai/gpt-5.4" },
    });
    fireEvent.click(screen.getByTestId("fleet-default-save"));

    const call = await vi.waitFor(() => {
      const found = calls.find((c) => c.method === "PATCH");
      expect(found).toBeDefined();
      return found!;
    });
    expect(call.url).toBe("/api/bridge/api/providers/defaults");
    expect(call.body).toEqual({
      model: "anthropic/claude-sonnet-4.6",
      fallbacks: ["openrouter/openai/gpt-5.4"],
    });
    expect(await screen.findByTestId("fleet-default-status")).toHaveTextContent(/in use|applied/i);
  });

  it("does not claim a saved default is live when the engine did not confirm", async () => {
    await renderPage({
      "PATCH /api/bridge/api/providers/defaults": {
        body: { model: "anthropic/claude-sonnet-4.6", fallbacks: [], applied: false },
      },
    });
    fireEvent.change(screen.getByTestId("fleet-default-model"), {
      target: { value: "anthropic/claude-sonnet-4.6" },
    });
    fireEvent.click(screen.getByTestId("fleet-default-save"));
    const status = await screen.findByTestId("fleet-default-status");
    expect(status).toHaveTextContent(/not.*(confirm|live|in use)|restart/i);
  });

  it("renders the server's rejection of an unknown model", async () => {
    await renderPage({
      "PATCH /api/bridge/api/providers/defaults": {
        status: 422,
        body: { detail: "unknown model id(s): anthropic/claude-sonnet-4.6" },
      },
    });
    fireEvent.change(screen.getByTestId("fleet-default-model"), {
      target: { value: "anthropic/claude-sonnet-4.6" },
    });
    fireEvent.click(screen.getByTestId("fleet-default-save"));
    expect(await screen.findByTestId("fleet-default-error")).toHaveTextContent(
      /unknown model id/
    );
  });

  it("says that agents pinning a model keep it", async () => {
    await renderPage();
    expect(screen.getByTestId("fleet-defaults")).toHaveTextContent(/pin/i);
  });
});
