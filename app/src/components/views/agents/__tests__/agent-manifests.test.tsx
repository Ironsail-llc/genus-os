/**
 * The agent builder, against the exact shapes
 * `crm/bridge/routers/agent_manifests.py` returns.
 *
 * The properties these tests are built around:
 *
 * * A PATCH carries only what the operator actually changed. The bridge writes
 *   every path it is handed, so sending the whole form back would rewrite
 *   fields nobody touched and bump the version for nothing.
 * * A 422 verdict lands beside the field named in its `path`. A refusal shown
 *   at the top of a form with fifteen inputs is a refusal the operator has to
 *   go hunting through.
 * * Retire refuses until the id is typed, mirroring the bridge's own
 *   `confirm must be the agent's id` — the client must not be the only lock,
 *   and it must not be a weaker one.
 * * A non-operator gets the list and no write affordances at all.
 */
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentManifests } from "../agent-manifests";

const MANIFESTS = {
  agents: [
    {
      id: "invoice-chaser",
      name: "Invoice Chaser",
      description: "Chases unpaid invoices every weekday morning.",
      version: "2026-09-01",
      department: "finance",
      cron: "0 9 * * 1-5",
      timezone: "UTC",
      enabled: true,
      delivery: "announce",
      model: "openrouter/openai/gpt-5.4",
    },
    {
      id: "lead-scout",
      name: "Lead Scout",
      description: "Reads the inbound queue and files leads.",
      version: "2026-08-14",
      department: "sales",
      cron: "",
      timezone: "",
      enabled: false,
      delivery: "none",
      model: "",
    },
  ],
  broken: [
    { id: "night-sweep", filename: "night-sweep.yaml", error_type: "ScannerError" },
  ],
  count: 2,
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
  ],
};

const DETAIL = {
  manifest: {
    id: "invoice-chaser",
    name: "Invoice Chaser",
    description: "Chases unpaid invoices every weekday morning.",
    version: "2026-09-01",
    department: "finance",
    instruction_file: "agents/invoice-chaser.md",
    model: { primary: "openrouter/openai/gpt-5.4", fallbacks: ["anthropic/claude-sonnet-4.6"] },
    schedule: { cron: "0 9 * * 1-5", timezone: "UTC", enabled: true },
    delivery: { mode: "announce", channel: "telegram", to: "ops" },
    tools_allowed: ["crm_search", "web_fetch"],
  },
  yaml: "id: invoice-chaser\nname: Invoice Chaser\n",
  instructions: "Chase every invoice past thirty days.",
  validation: { ok: true, errors: [], warnings: [] },
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

const LIST = "GET /api/bridge/api/agent-manifests";

function installFetch(routes: Record<string, Route> = {}) {
  const table: Record<string, Route> = {
    [LIST]: { body: MANIFESTS },
    "GET /api/bridge/api/models": { body: MODELS },
    "GET /api/bridge/api/agent-manifests/invoice-chaser": { body: DETAIL },
    ...routes,
  };
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined;
    calls.push({ url, method, body });
    const route = table[`${method} ${url}`];
    if (!route) {
      return {
        ok: false,
        status: 404,
        json: async () => ({ detail: `no route for ${method} ${url}` }),
      } as Response;
    }
    const reply = typeof route === "function" ? route(body) : route;
    const status = reply.status ?? 200;
    return { ok: status < 400, status, json: async () => reply.body } as Response;
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  calls.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function renderPage(routes: Record<string, Route> = {}, role = "owner") {
  const mock = installFetch(routes);
  render(<AgentManifests role={role} roleLoading={false} />);
  await screen.findByTestId("agent-row-invoice-chaser");
  return mock;
}

function callsTo(method: string, url: string): Call[] {
  return calls.filter((call) => call.method === method && call.url === url);
}

async function openCreate() {
  fireEvent.click(screen.getByTestId("agent-new"));
  return screen.findByTestId("agent-panel");
}

function fillThreeFields() {
  fireEvent.change(screen.getByTestId("agent-field-name"), {
    target: { value: "Vendor Follow Up" },
  });
  fireEvent.change(screen.getByTestId("agent-field-job"), {
    target: { value: "Chase vendors who have gone quiet." },
  });
  fireEvent.change(screen.getByTestId("agent-field-instructions"), {
    target: { value: "Open the vendor list and write to anyone silent for a week." },
  });
}

describe("AgentManifests — the listing", () => {
  it("says it is reading the fleet before the bridge answers", () => {
    installFetch();
    render(<AgentManifests role="owner" roleLoading={false} />);
    expect(screen.getByTestId("agent-manifests-loading")).toBeInTheDocument();
  });

  it("renders one row per manifest with its id, job, schedule, model and delivery", async () => {
    await renderPage();
    const row = screen.getByTestId("agent-row-invoice-chaser");
    expect(row).toHaveTextContent("Invoice Chaser");
    expect(row).toHaveTextContent("invoice-chaser");
    expect(row).toHaveTextContent("Chases unpaid invoices");
    expect(row).toHaveTextContent("0 9 * * 1-5");
    expect(row).toHaveTextContent("UTC");
    expect(row).toHaveTextContent("openrouter/openai/gpt-5.4");
    expect(row).toHaveTextContent(/announce/);
  });

  it("puts the cron in words beside the expression", async () => {
    await renderPage();
    expect(screen.getByTestId("agent-cron-human-invoice-chaser")).toHaveTextContent(/Monday/i);
  });

  it("shows enabled and disabled as different pills", async () => {
    await renderPage();
    expect(screen.getByTestId("agent-enabled-invoice-chaser")).toHaveTextContent(/^Enabled$/);
    expect(screen.getByTestId("agent-enabled-lead-scout")).toHaveTextContent(/^Disabled$/);
  });

  it("says an agent with no cron is trigger-only rather than leaving the cell blank", async () => {
    await renderPage();
    expect(screen.getByTestId("agent-row-lead-scout")).toHaveTextContent(/only runs when triggered/i);
  });

  it("lists the manifests that will not load, with filename and error type", async () => {
    await renderPage();
    const broken = screen.getByTestId("agent-manifests-broken");
    expect(broken).toHaveTextContent("night-sweep.yaml");
    expect(broken).toHaveTextContent("ScannerError");
  });

  it("hides the broken section entirely when every manifest loads", async () => {
    installFetch({ [LIST]: { body: { ...MANIFESTS, broken: [] } } });
    render(<AgentManifests role="owner" roleLoading={false} />);
    await screen.findByTestId("agent-row-invoice-chaser");
    expect(screen.queryByTestId("agent-manifests-broken")).not.toBeInTheDocument();
  });

  it("says the appliance has no agents rather than showing an empty table", async () => {
    installFetch({ [LIST]: { body: { agents: [], broken: [], count: 0 } } });
    render(<AgentManifests role="owner" roleLoading={false} />);
    expect(await screen.findByTestId("agent-manifests-empty")).toBeInTheDocument();
  });

  it("renders the bridge's own sentence when the listing fails", async () => {
    installFetch({
      [LIST]: { status: 502, body: { detail: "the engine did not answer" } },
    });
    render(<AgentManifests role="owner" roleLoading={false} />);
    expect(await screen.findByTestId("agent-manifests-error")).toHaveTextContent(
      "the engine did not answer"
    );
  });
});

describe("AgentManifests — creating", () => {
  it("shows the id the bridge will derive, as the name is typed", async () => {
    await renderPage();
    await openCreate();
    fireEvent.change(screen.getByTestId("agent-field-name"), {
      target: { value: "Vendor Follow Up" },
    });
    expect(screen.getByTestId("agent-derived-id")).toHaveTextContent("vendor-follow-up");
    expect(screen.getByTestId("agent-derived-id")).toHaveTextContent(/fixed after creation/i);
  });

  it("posts exactly the three fields it was given", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 201,
        body: {
          id: "vendor-follow-up",
          manifest: { id: "vendor-follow-up" },
          warnings: [],
          reconcile: { applied: true },
        },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create"));

    await waitFor(() => expect(callsTo("POST", "/api/bridge/api/agent-manifests")).toHaveLength(1));
    expect(callsTo("POST", "/api/bridge/api/agent-manifests")[0].body).toEqual({
      name: "Vendor Follow Up",
      description: "Chase vendors who have gone quiet.",
      instructions: "Open the vendor list and write to anyone silent for a week.",
    });
  });

  it("reports what the bridge answered, including the engine's reconcile", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 201,
        body: {
          id: "vendor-follow-up",
          manifest: { id: "vendor-follow-up" },
          warnings: [{ path: "model.primary", code: "unset", message: "inherits the fleet model" }],
          reconcile: { applied: true },
        },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create"));

    const result = await screen.findByTestId("agent-panel-result");
    expect(result).toHaveTextContent("vendor-follow-up");
    expect(screen.getByTestId("agent-panel-warnings")).toHaveTextContent(
      "inherits the fleet model"
    );
  });

  it("warns when the manifest was written but the engine did not reconcile", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 201,
        body: {
          id: "vendor-follow-up",
          manifest: {},
          warnings: [],
          reconcile: { applied: false, error: "the engine did not reconcile" },
        },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create"));

    expect(await screen.findByTestId("agent-panel-reconcile")).toHaveTextContent(
      "the engine did not reconcile"
    );
  });

  it("creates and then triggers once when asked for both", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 201,
        body: { id: "vendor-follow-up", manifest: {}, warnings: [], reconcile: { applied: true } },
      },
      "POST /api/bridge/api/agent-manifests/vendor-follow-up/run": {
        body: { id: "vendor-follow-up", triggered: { run_id: "r-1" } },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create-run"));

    await waitFor(() =>
      expect(
        callsTo("POST", "/api/bridge/api/agent-manifests/vendor-follow-up/run")
      ).toHaveLength(1)
    );
    // The id the RUN uses is the one the server answered with, not the one the
    // form derived: the two agree today and the server's is the authority.
    expect(await screen.findByTestId("agent-panel-result")).toHaveTextContent(/triggered/i);
  });

  it("does not trigger a run when the create itself was refused", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 409,
        body: { detail: "an agent with that id already exists" },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create-run"));

    await screen.findByTestId("agent-error-id");
    expect(calls.filter((call) => call.url.endsWith("/run"))).toHaveLength(0);
  });

  it("lands a 422 verdict on the field its path names", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 422,
        body: {
          detail: {
            ok: false,
            errors: [
              { path: "schedule.cron", code: "bad_cron", message: "five fields, not three" },
              { path: "name", code: "too_long", message: "a name is at most 120 characters" },
            ],
            warnings: [],
          },
        },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create"));

    expect(await screen.findByTestId("agent-error-cron")).toHaveTextContent(
      "five fields, not three"
    );
    expect(screen.getByTestId("agent-error-name")).toHaveTextContent("at most 120 characters");
  });

  it("lists the errors whose path no field on this form owns", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 422,
        body: {
          detail: {
            ok: false,
            errors: [{ path: "v2.sandbox.image", code: "unknown", message: "no such image" }],
            warnings: [],
          },
        },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create"));

    const other = await screen.findByTestId("agent-errors-other");
    expect(other).toHaveTextContent("v2.sandbox.image");
    expect(other).toHaveTextContent("no such image");
  });

  it("offers the id field when the bridge says that id is taken", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 409,
        body: { detail: "an agent with that id already exists" },
      },
    });
    await openCreate();
    fillThreeFields();
    fireEvent.click(screen.getByTestId("agent-create"));

    expect(await screen.findByTestId("agent-error-id")).toHaveTextContent(/already exists/i);
    const idField = screen.getByTestId("agent-field-id");
    fireEvent.change(idField, { target: { value: "vendor-follow-up-2" } });
    fireEvent.click(screen.getByTestId("agent-create"));

    await waitFor(() => expect(callsTo("POST", "/api/bridge/api/agent-manifests")).toHaveLength(2));
    expect(callsTo("POST", "/api/bridge/api/agent-manifests")[1].body).toMatchObject({
      id: "vendor-follow-up-2",
    });
  });

  it("refuses to post a name that derives to no id at all", async () => {
    await renderPage();
    await openCreate();
    fireEvent.change(screen.getByTestId("agent-field-name"), { target: { value: "!!!" } });
    expect(screen.getByTestId("agent-create")).toBeDisabled();
    expect(screen.getByTestId("agent-derived-id")).toHaveTextContent(/no id can be derived/i);
  });
});

describe("AgentManifests — the Advanced drawer", () => {
  async function openAdvanced() {
    await openCreate();
    fireEvent.click(screen.getByTestId("agent-advanced-toggle"));
    await screen.findByTestId("agent-advanced");
  }

  it("is closed until it is asked for", async () => {
    await renderPage();
    await openCreate();
    expect(screen.queryByTestId("agent-advanced")).not.toBeInTheDocument();
  });

  it("groups the model choices by the provider that bills them", async () => {
    await renderPage();
    await openAdvanced();
    const select = screen.getByTestId("agent-field-model");
    expect(within(select).getByRole("group", { name: "openrouter" })).toBeInTheDocument();
    expect(within(select).getByRole("group", { name: "anthropic" })).toBeInTheDocument();
  });

  it("says an empty model means the fleet default rather than no model", async () => {
    await renderPage();
    await openAdvanced();
    expect(screen.getByTestId("agent-model-hint")).toHaveTextContent(/fleet default/i);
  });

  it("previews the cron in words and names the next run", async () => {
    await renderPage();
    await openAdvanced();
    fireEvent.change(screen.getByTestId("agent-field-cron"), { target: { value: "0 9 * * 1-5" } });
    expect(screen.getByTestId("agent-cron-preview")).toHaveTextContent(/Monday/i);
    expect(screen.getByTestId("agent-cron-next")).toHaveTextContent(/20\d\d/);
  });

  it("says a bad cron is not a schedule without throwing", async () => {
    await renderPage();
    await openAdvanced();
    fireEvent.change(screen.getByTestId("agent-field-cron"), { target: { value: "0 99 * * *" } });
    expect(screen.getByTestId("agent-cron-preview")).toHaveTextContent("not a valid schedule");
    expect(screen.queryByTestId("agent-cron-next")).not.toBeInTheDocument();
  });

  it("posts the advanced fields in the create body", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests": {
        status: 201,
        body: { id: "vendor-follow-up", manifest: {}, warnings: [], reconcile: { applied: true } },
      },
    });
    await openAdvanced();
    fillThreeFields();
    fireEvent.change(screen.getByTestId("agent-field-model"), {
      target: { value: "anthropic/claude-sonnet-4.6" },
    });
    fireEvent.click(screen.getByTestId("agent-fallback-add"));
    fireEvent.change(screen.getByTestId("agent-fallback-0"), {
      target: { value: "openrouter/openai/gpt-5.4" },
    });
    fireEvent.change(screen.getByTestId("agent-field-cron"), { target: { value: "0 9 * * 1-5" } });
    fireEvent.change(screen.getByTestId("agent-field-timezone"), { target: { value: "UTC" } });
    fireEvent.change(screen.getByTestId("agent-field-deliveryMode"), {
      target: { value: "announce" },
    });
    fireEvent.change(screen.getByTestId("agent-field-deliveryChannel"), {
      target: { value: "telegram" },
    });
    fireEvent.change(screen.getByTestId("agent-field-deliveryTo"), { target: { value: "ops" } });
    fireEvent.change(screen.getByTestId("agent-tool-input"), { target: { value: "crm_search" } });
    fireEvent.click(screen.getByTestId("agent-tool-add"));
    fireEvent.click(screen.getByTestId("agent-create"));

    await waitFor(() => expect(callsTo("POST", "/api/bridge/api/agent-manifests")).toHaveLength(1));
    expect(callsTo("POST", "/api/bridge/api/agent-manifests")[0].body).toEqual({
      name: "Vendor Follow Up",
      description: "Chase vendors who have gone quiet.",
      instructions: "Open the vendor list and write to anyone silent for a week.",
      model: "anthropic/claude-sonnet-4.6",
      fallbacks: ["openrouter/openai/gpt-5.4"],
      cron: "0 9 * * 1-5",
      timezone: "UTC",
      delivery_mode: "announce",
      delivery_channel: "telegram",
      delivery_to: "ops",
      tools_allowed: ["crm_search"],
    });
  });

  it("keeps tool names as removable chips", async () => {
    await renderPage();
    await openAdvanced();
    fireEvent.change(screen.getByTestId("agent-tool-input"), { target: { value: "web_fetch" } });
    fireEvent.click(screen.getByTestId("agent-tool-add"));
    expect(screen.getByTestId("agent-tool-0")).toHaveTextContent("web_fetch");
    fireEvent.click(screen.getByTestId("agent-tool-remove-0"));
    expect(screen.queryByTestId("agent-tool-0")).not.toBeInTheDocument();
  });

  it("does not add the same tool twice", async () => {
    await renderPage();
    await openAdvanced();
    const input = screen.getByTestId("agent-tool-input");
    fireEvent.change(input, { target: { value: "web_fetch" } });
    fireEvent.click(screen.getByTestId("agent-tool-add"));
    fireEvent.change(input, { target: { value: "web_fetch" } });
    fireEvent.click(screen.getByTestId("agent-tool-add"));
    expect(screen.getByTestId("agent-tool-0")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-tool-1")).not.toBeInTheDocument();
  });
});

describe("AgentManifests — editing", () => {
  async function openEdit() {
    await renderPageForEdit();
    fireEvent.click(screen.getByTestId("agent-open-invoice-chaser"));
    await screen.findByTestId("agent-panel");
  }

  async function renderPageForEdit(routes: Record<string, Route> = {}) {
    return renderPage(routes);
  }

  it("prefills the panel from the manifest the bridge holds", async () => {
    await openEdit();
    await waitFor(() =>
      expect(screen.getByTestId("agent-field-name")).toHaveValue("Invoice Chaser")
    );
    fireEvent.click(screen.getByTestId("agent-advanced-toggle"));
    expect(screen.getByTestId("agent-field-cron")).toHaveValue("0 9 * * 1-5");
    expect(screen.getByTestId("agent-field-model")).toHaveValue("openrouter/openai/gpt-5.4");
    expect(screen.getByTestId("agent-fallback-0")).toHaveValue("anthropic/claude-sonnet-4.6");
    expect(screen.getByTestId("agent-field-deliveryChannel")).toHaveValue("telegram");
    expect(screen.getByTestId("agent-tool-0")).toHaveTextContent("crm_search");
  });

  it("shows the instructions read-only and names the file that holds them", async () => {
    await openEdit();
    const instructions = await screen.findByTestId("agent-field-instructions");
    expect(instructions).toHaveAttribute("readonly");
    expect(screen.getByTestId("agent-instructions-note")).toHaveTextContent(
      "agents/invoice-chaser.md"
    );
  });

  it("PATCHes only the fields that actually changed", async () => {
    await renderPage({
      "PATCH /api/bridge/api/agent-manifests/invoice-chaser": {
        body: {
          id: "invoice-chaser",
          manifest: { version: "2026-09-14" },
          warnings: [],
          pre_existing: [],
          reconcile: { applied: true },
        },
      },
    });
    fireEvent.click(screen.getByTestId("agent-open-invoice-chaser"));
    await waitFor(() =>
      expect(screen.getByTestId("agent-field-name")).toHaveValue("Invoice Chaser")
    );
    fireEvent.click(screen.getByTestId("agent-advanced-toggle"));
    fireEvent.change(screen.getByTestId("agent-field-cron"), { target: { value: "0 7 * * 1-5" } });
    fireEvent.click(screen.getByTestId("agent-save"));

    await waitFor(() =>
      expect(callsTo("PATCH", "/api/bridge/api/agent-manifests/invoice-chaser")).toHaveLength(1)
    );
    expect(callsTo("PATCH", "/api/bridge/api/agent-manifests/invoice-chaser")[0].body).toEqual({
      cron: "0 7 * * 1-5",
      change: "Edited via the Helm agent builder",
    });
  });

  it("sends the change note the operator wrote", async () => {
    await renderPage({
      "PATCH /api/bridge/api/agent-manifests/invoice-chaser": {
        body: { id: "invoice-chaser", manifest: {}, warnings: [], reconcile: { applied: true } },
      },
    });
    fireEvent.click(screen.getByTestId("agent-open-invoice-chaser"));
    await waitFor(() =>
      expect(screen.getByTestId("agent-field-name")).toHaveValue("Invoice Chaser")
    );
    fireEvent.change(screen.getByTestId("agent-field-name"), { target: { value: "Invoice Hound" } });
    fireEvent.change(screen.getByTestId("agent-field-change"), {
      target: { value: "Renamed after the department merge" },
    });
    fireEvent.click(screen.getByTestId("agent-save"));

    await waitFor(() =>
      expect(callsTo("PATCH", "/api/bridge/api/agent-manifests/invoice-chaser")).toHaveLength(1)
    );
    expect(callsTo("PATCH", "/api/bridge/api/agent-manifests/invoice-chaser")[0].body).toEqual({
      name: "Invoice Hound",
      change: "Renamed after the department merge",
    });
  });

  it("refuses to save when nothing has changed, rather than bumping the version", async () => {
    await renderPage();
    fireEvent.click(screen.getByTestId("agent-open-invoice-chaser"));
    await waitFor(() =>
      expect(screen.getByTestId("agent-field-name")).toHaveValue("Invoice Chaser")
    );
    expect(screen.getByTestId("agent-save")).toBeDisabled();
  });

  it("reports the version the save produced", async () => {
    await renderPage({
      "PATCH /api/bridge/api/agent-manifests/invoice-chaser": {
        body: {
          id: "invoice-chaser",
          manifest: { version: "2026-09-14" },
          warnings: [{ path: "tools_allowed", code: "unknown_tool", message: "no tool named x" }],
          pre_existing: [],
          reconcile: { applied: true },
        },
      },
    });
    fireEvent.click(screen.getByTestId("agent-open-invoice-chaser"));
    await waitFor(() =>
      expect(screen.getByTestId("agent-field-name")).toHaveValue("Invoice Chaser")
    );
    fireEvent.change(screen.getByTestId("agent-field-name"), { target: { value: "Invoice Hound" } });
    fireEvent.click(screen.getByTestId("agent-save"));

    expect(await screen.findByTestId("agent-panel-result")).toHaveTextContent("2026-09-14");
    expect(screen.getByTestId("agent-panel-warnings")).toHaveTextContent("no tool named x");
  });

  it("validates the YAML tab through the validate route and renders the verdict", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests/validate": {
        body: {
          ok: false,
          errors: [{ path: "schedule.cron", code: "bad_cron", message: "five fields, not three" }],
          warnings: [],
        },
      },
    });
    fireEvent.click(screen.getByTestId("agent-open-invoice-chaser"));
    await waitFor(() =>
      expect(screen.getByTestId("agent-field-name")).toHaveValue("Invoice Chaser")
    );
    fireEvent.click(screen.getByTestId("agent-advanced-toggle"));
    fireEvent.click(screen.getByTestId("agent-tab-yaml"));
    expect(screen.getByTestId("agent-yaml")).toHaveTextContent("id: invoice-chaser");

    fireEvent.click(screen.getByTestId("agent-validate"));
    await waitFor(() =>
      expect(callsTo("POST", "/api/bridge/api/agent-manifests/validate")).toHaveLength(1)
    );
    // The YAML pane sends the yaml, never a manifest it re-serialised itself.
    expect(callsTo("POST", "/api/bridge/api/agent-manifests/validate")[0].body).toEqual({
      yaml: DETAIL.yaml,
    });
    expect(await screen.findByTestId("agent-validate-result")).toHaveTextContent(
      "five fields, not three"
    );
  });
});

describe("AgentManifests — row actions", () => {
  it("disables an enabled agent through the disable route", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests/invoice-chaser/disable": {
        body: { id: "invoice-chaser", manifest: {}, warnings: [], reconcile: { applied: true } },
      },
    });
    fireEvent.click(screen.getByTestId("agent-toggle-invoice-chaser"));
    await waitFor(() =>
      expect(
        callsTo("POST", "/api/bridge/api/agent-manifests/invoice-chaser/disable")
      ).toHaveLength(1)
    );
  });

  it("enables a disabled agent through the enable route", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests/lead-scout/enable": {
        body: { id: "lead-scout", manifest: {}, warnings: [], reconcile: { applied: true } },
      },
    });
    fireEvent.click(screen.getByTestId("agent-toggle-lead-scout"));
    await waitFor(() =>
      expect(callsTo("POST", "/api/bridge/api/agent-manifests/lead-scout/enable")).toHaveLength(1)
    );
  });

  it("runs an agent now and shows what the engine answered", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests/invoice-chaser/run": {
        body: { id: "invoice-chaser", triggered: { run_id: "r-42" } },
      },
    });
    fireEvent.click(screen.getByTestId("agent-run-invoice-chaser"));
    expect(await screen.findByTestId("agent-note-invoice-chaser")).toHaveTextContent("r-42");
  });

  it("shows the bridge's refusal when the engine would not take the trigger", async () => {
    await renderPage({
      "POST /api/bridge/api/agent-manifests/invoice-chaser/run": {
        status: 502,
        body: { detail: "the engine did not accept the trigger" },
      },
    });
    fireEvent.click(screen.getByTestId("agent-run-invoice-chaser"));
    expect(await screen.findByTestId("agent-error-invoice-chaser")).toHaveTextContent(
      "the engine did not accept the trigger"
    );
  });

  it("will not retire until the id has been typed exactly", async () => {
    await renderPage({
      "DELETE /api/bridge/api/agent-manifests/invoice-chaser": {
        body: { id: "invoice-chaser", retired: true, reconcile: { applied: true } },
      },
    });
    fireEvent.click(screen.getByTestId("agent-retire-invoice-chaser"));
    const confirm = await screen.findByTestId("agent-retire-confirm-invoice-chaser");
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByTestId("agent-retire-input-invoice-chaser"), {
      target: { value: "invoice-chase" },
    });
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByTestId("agent-retire-input-invoice-chaser"), {
      target: { value: "invoice-chaser" },
    });
    expect(confirm).toBeEnabled();
  });

  it("retires with the id in the body, drops the row and says where the file went", async () => {
    await renderPage({
      "DELETE /api/bridge/api/agent-manifests/invoice-chaser": {
        body: { id: "invoice-chaser", retired: true, reconcile: { applied: true } },
      },
      [LIST]: (): Reply => ({
        body: calls.some((call) => call.method === "DELETE")
          ? { ...MANIFESTS, agents: MANIFESTS.agents.filter((a) => a.id !== "invoice-chaser") }
          : MANIFESTS,
      }),
    });
    fireEvent.click(screen.getByTestId("agent-retire-invoice-chaser"));
    fireEvent.change(await screen.findByTestId("agent-retire-input-invoice-chaser"), {
      target: { value: "invoice-chaser" },
    });
    fireEvent.click(screen.getByTestId("agent-retire-confirm-invoice-chaser"));

    await waitFor(() =>
      expect(callsTo("DELETE", "/api/bridge/api/agent-manifests/invoice-chaser")).toHaveLength(1)
    );
    expect(callsTo("DELETE", "/api/bridge/api/agent-manifests/invoice-chaser")[0].body).toEqual({
      confirm: "invoice-chaser",
    });
    await waitFor(() =>
      expect(screen.queryByTestId("agent-row-invoice-chaser")).not.toBeInTheDocument()
    );
    expect(screen.getByTestId("agent-manifests-notice")).toHaveTextContent(/retired/i);
  });

  it("never reaches for a browser dialog", async () => {
    const confirmSpy = vi.fn(() => true);
    const promptSpy = vi.fn(() => "invoice-chaser");
    vi.stubGlobal("confirm", confirmSpy);
    vi.stubGlobal("prompt", promptSpy);
    await renderPage({
      "DELETE /api/bridge/api/agent-manifests/invoice-chaser": {
        body: { id: "invoice-chaser", retired: true, reconcile: { applied: true } },
      },
    });
    fireEvent.click(screen.getByTestId("agent-retire-invoice-chaser"));
    fireEvent.change(await screen.findByTestId("agent-retire-input-invoice-chaser"), {
      target: { value: "invoice-chaser" },
    });
    fireEvent.click(screen.getByTestId("agent-retire-confirm-invoice-chaser"));
    await waitFor(() =>
      expect(callsTo("DELETE", "/api/bridge/api/agent-manifests/invoice-chaser")).toHaveLength(1)
    );
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(promptSpy).not.toHaveBeenCalled();
  });
});

describe("AgentManifests — role gating", () => {
  it("gives a member the list and no way to change anything", async () => {
    await renderPage({}, "member");
    expect(screen.getByTestId("agent-row-invoice-chaser")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-new")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-open-invoice-chaser")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-run-invoice-chaser")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-toggle-invoice-chaser")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-retire-invoice-chaser")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-manifests-readonly")).toBeInTheDocument();
  });

  it("gives an admin the same affordances as the owner", async () => {
    await renderPage({}, "admin");
    expect(screen.getByTestId("agent-new")).toBeInTheDocument();
    expect(screen.getByTestId("agent-run-invoice-chaser")).toBeInTheDocument();
  });

  it("does not call an unresolved session a refusal", async () => {
    installFetch();
    render(<AgentManifests role={undefined} roleLoading />);
    await screen.findByTestId("agent-row-invoice-chaser");
    // Loading is not "denied": the owner must not be shown a read-only screen
    // for the half second before their own session resolves.
    expect(screen.queryByTestId("agent-manifests-readonly")).not.toBeInTheDocument();
    expect(screen.queryByTestId("agent-new")).not.toBeInTheDocument();
  });
});

describe("AgentManifests — merged with fleet health", () => {
  const HEALTH = [
    {
      name: "Invoice Chaser",
      agentId: "invoice-chaser",
      schedule: "0 9 * * 1-5",
      status: "degraded" as const,
      lastRun: "2026-09-13T09:00:00Z",
    },
  ];

  it("shows the health tier the engine reports for that agent", async () => {
    installFetch();
    render(<AgentManifests role="owner" roleLoading={false} health={HEALTH} />);
    await screen.findByTestId("agent-row-invoice-chaser");
    expect(screen.getByTestId("agent-health-invoice-chaser")).toHaveTextContent(/degraded/i);
  });

  it("matches on the agent id, never on the display name", async () => {
    // The manifest `name` is a display string an operator renames freely; the
    // id is the key both sides agree on. A name-matched merge puts one agent's
    // health on another agent's row the first time somebody renames one.
    installFetch();
    render(
      <AgentManifests
        role="owner"
        roleLoading={false}
        health={[{ ...HEALTH[0], agentId: "lead-scout", name: "Invoice Chaser" }]}
      />
    );
    await screen.findByTestId("agent-row-invoice-chaser");
    expect(screen.getByTestId("agent-health-lead-scout")).toHaveTextContent(/degraded/i);
    expect(screen.getByTestId("agent-health-invoice-chaser")).toHaveTextContent(/no runs/i);
  });

  it("says an agent the engine has never run has no health rather than failing", async () => {
    installFetch();
    render(<AgentManifests role="owner" roleLoading={false} health={[]} />);
    await screen.findByTestId("agent-row-invoice-chaser");
    expect(screen.getByTestId("agent-health-invoice-chaser")).toHaveTextContent(/no runs/i);
  });
});
