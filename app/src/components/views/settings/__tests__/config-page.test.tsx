/**
 * Settings › Config, as the operator drives it.
 *
 * Every shape below is the bridge's own, read off `crm/bridge/routers/
 * settings.py` — the schema groups, the values map, the flat
 * `{applied, pending_restart, errors}` that both the 200 and the 422 answer
 * with. Inventing a field in a fixture is how a page ships against an API that
 * does not exist, so nothing here is invented.
 *
 * The claims that matter are the ones about NOT lying:
 *
 * * a secret never gets an input and its value never reaches the screen — the
 *   route answers `{configured, fingerprint}` and that is all there is;
 * * a field the environment supplies is read-only, and the badge says why,
 *   because the bridge refuses the write with that exact reason;
 * * a save posts ONLY what changed — the route is all-or-nothing, so sending
 *   371 unchanged fields means one unrelated bad value blocks the edit;
 * * a 422 lands on the fields, and NOTHING is marked saved, because nothing
 *   was written;
 * * the restart banner names the units the PATCH reported, and survives the
 *   operator navigating away — the API keeps no memory of it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConfigPage } from "../config-page";
import { resetRestartNotice } from "@/lib/settings/restart-banner";

const SCHEMA = {
  groups: [
    {
      id: "engine",
      label: "Engine",
      fields: [
        {
          name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          env: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          field: "engine.max_concurrent_agents",
          group: "engine",
          aliases: [],
          type: "int",
          description: "How many agent runs may execute at once.",
          default: 3,
          secret: false,
          governed: false,
          restart_required: true,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: false,
        },
        {
          name: "ROBOTHOR_LOG_DIR",
          env: "ROBOTHOR_LOG_DIR",
          field: "engine.log_dir",
          group: "engine",
          aliases: [],
          type: "str",
          description: "Where the engine writes its logs.",
          default: "/var/log/robothor",
          secret: false,
          governed: false,
          restart_required: true,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: false,
        },
        {
          name: "ROBOTHOR_DETECTORS_ENABLED",
          env: "ROBOTHOR_DETECTORS_ENABLED",
          field: "engine.detectors_enabled",
          group: "engine",
          aliases: [],
          type: "bool",
          description: "Run the background detectors.",
          default: true,
          secret: false,
          governed: false,
          restart_required: false,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: true,
        },
        {
          name: "ROBOTHOR_ENGINE_HOST",
          env: "ROBOTHOR_ENGINE_HOST",
          field: "engine.host",
          group: "engine",
          aliases: [],
          type: "str",
          description: "Address the engine binds.",
          default: "127.0.0.1",
          secret: false,
          governed: false,
          restart_required: true,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: false,
        },
      ],
    },
    {
      id: "channels",
      label: "Channels",
      fields: [
        {
          name: "ROBOTHOR_TELEGRAM_BOT_TOKEN",
          env: "ROBOTHOR_TELEGRAM_BOT_TOKEN",
          field: "channels.telegram_bot_token",
          group: "channels",
          aliases: [],
          type: "str",
          description: "The Telegram bot's API token.",
          default: null,
          secret: true,
          governed: false,
          restart_required: true,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: false,
        },
      ],
    },
    {
      id: "flags",
      label: "Flags",
      fields: [
        {
          name: "ROBOTHOR_RBAC_MODE",
          env: "ROBOTHOR_RBAC_MODE",
          field: "flags.rbac_mode",
          group: "flags",
          aliases: [],
          type: "str",
          description: "RBAC ladder position.",
          default: "observe",
          secret: false,
          governed: true,
          restart_required: false,
          restart_units: ["robothor-engine"],
          since: "legacy",
          hot: true,
          enum: ["off", "observe", "alert", "enforce"],
        },
      ],
    },
  ],
};

/**
 * The refusal the route itself would answer a PATCH of ROBOTHOR_ENGINE_HOST
 * with, verbatim.
 *
 * It names `ROBOTHOR_OLD_ENGINE_HOST` — a DEPRECATED ALIAS — on purpose.
 * `provenance.env_name_in_use` walks `(env, *aliases)`, so on a mid-migration
 * box the variable actually supplying a field is not the canonical name. A
 * page that composed this sentence itself would tell the operator to clear a
 * variable that is not set, and the field would still be overridden after the
 * restart.
 */
const ENV_REFUSAL =
  "ROBOTHOR_ENGINE_HOST is set in this instance's environment (ROBOTHOR_OLD_ENGINE_HOST), " +
  "which wins over config.yaml — a change saved here would apply to nothing. Clear the " +
  "variable on the box and restart robothor-engine, then it can be managed from this page.";

const SECRET_REFUSAL =
  "ROBOTHOR_TELEGRAM_BOT_TOKEN holds a credential. `genus config set` never writes secrets -- " +
  "config.yaml is a plain file that gets copied into bug reports. Store it with " +
  "`genus vault set <key>` and give the service the key; `genus vault list` shows the naming in use.";

const VALUES = {
  values: {
    ROBOTHOR_MAX_CONCURRENT_AGENTS: {
      value: 3,
      source: "default",
      editable: true,
      reason: null,
    },
    ROBOTHOR_LOG_DIR: {
      value: "/var/log/robothor",
      source: "config",
      editable: true,
      reason: null,
    },
    ROBOTHOR_DETECTORS_ENABLED: { value: true, source: "default", editable: true, reason: null },
    ROBOTHOR_ENGINE_HOST: {
      value: "0.0.0.0",
      source: "env",
      editable: false,
      reason: ENV_REFUSAL,
    },
    ROBOTHOR_TELEGRAM_BOT_TOKEN: {
      value: { configured: true, fingerprint: "sha256:ab12cd34" },
      source: "env",
      editable: false,
      reason: SECRET_REFUSAL,
    },
    // Governed, and editable WHATEVER supplied it: the flag store reads its
    // operator row before the environment, so the write is never invisible.
    ROBOTHOR_RBAC_MODE: { value: "enforce", source: "db", editable: true, reason: null },
  },
  pending_restart: [],
};

interface Recorded {
  patches: Array<Record<string, unknown>>;
}

type Reply = { status: number; body: unknown };

function mockBridge(patchReply: () => Reply = () => ok()): Recorded {
  const recorded: Recorded = { patches: [] };
  vi.spyOn(global, "fetch").mockImplementation((async (
    input: RequestInfo | URL,
    init?: RequestInit
  ) => {
    const url = String(input);
    if (init?.method === "PATCH") {
      recorded.patches.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      const reply = patchReply();
      return {
        ok: reply.status < 400,
        status: reply.status,
        json: async () => reply.body,
      } as Response;
    }
    const body = url.includes("/schema") ? SCHEMA : VALUES;
    return { ok: true, status: 200, json: async () => body } as Response;
  }) as typeof fetch);
  return recorded;
}

function ok(applied: string[] = [], pending: string[] = []): Reply {
  return { status: 200, body: { applied, pending_restart: pending, errors: [] } };
}

function refused(errors: Array<{ name: string; message: string }>): Reply {
  return { status: 422, body: { applied: [], pending_restart: [], errors } };
}

/** The Engine group is the one every test edits; open it and wait for the rows. */
async function openEngine() {
  fireEvent.click(await screen.findByTestId("config-group-toggle-engine"));
  return screen.findByTestId("config-field-ROBOTHOR_LOG_DIR");
}

beforeEach(() => {
  resetRestartNotice();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Settings › Config", () => {
  it("reads the schema and the values once each, and never polls", async () => {
    vi.useFakeTimers();
    try {
      mockBridge();
      render(<ConfigPage visible />);
      await vi.waitFor(() => expect(screen.getByTestId("config-group-engine")).toBeInTheDocument());
      const before = vi.mocked(global.fetch).mock.calls.length;
      expect(before).toBe(2);
      // The schema is 164 KB and the form holds the operator's unsaved edits;
      // a poll would re-read both under their cursor.
      await vi.advanceTimersByTimeAsync(180_000);
      expect(vi.mocked(global.fetch).mock.calls.length).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders a section per schema group, collapsed until asked", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    for (const id of ["engine", "channels", "flags"]) {
      expect(await screen.findByTestId(`config-group-${id}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("config-field-ROBOTHOR_LOG_DIR")).not.toBeInTheDocument();
    await openEngine();
    expect(screen.getByTestId("config-field-ROBOTHOR_LOG_DIR")).toBeInTheDocument();
  });

  it("renders each field by its declared type", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    await openEngine();

    const number = screen.getByTestId("config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS");
    expect(number).toHaveAttribute("type", "number");
    expect(number).toHaveValue(3);

    expect(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR")).toHaveAttribute("type", "text");

    const toggle = screen.getByTestId("config-switch-ROBOTHOR_DETECTORS_ENABLED");
    expect(toggle).toHaveAttribute("role", "switch");
    expect(toggle).toHaveAttribute("aria-checked", "true");
  });

  it("renders a bounded field as a select offering exactly the values the write accepts", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-flags"));
    const select = await screen.findByTestId("config-select-ROBOTHOR_RBAC_MODE");
    expect(Array.from(select.querySelectorAll("option")).map((o) => o.textContent)).toEqual([
      "off",
      "observe",
      "alert",
      "enforce",
    ]);
  });

  it("shows the env name and the layer that supplied each value", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    await openEngine();
    const row = screen.getByTestId("config-field-ROBOTHOR_LOG_DIR");
    expect(within(row).getByText("ROBOTHOR_LOG_DIR")).toBeInTheDocument();
    expect(screen.getByTestId("config-source-ROBOTHOR_LOG_DIR")).toHaveTextContent("config.yaml");
    expect(screen.getByTestId("config-source-ROBOTHOR_MAX_CONCURRENT_AGENTS")).toHaveTextContent(
      "default"
    );
  });

  it("never gives a secret an input, and never shows its value", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-channels"));
    const row = await screen.findByTestId("config-field-ROBOTHOR_TELEGRAM_BOT_TOKEN");
    expect(within(row).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(row).queryByRole("switch")).not.toBeInTheDocument();
    expect(within(row).queryByRole("combobox")).not.toBeInTheDocument();
    const status = screen.getByTestId("config-secret-ROBOTHOR_TELEGRAM_BOT_TOKEN");
    expect(status).toHaveTextContent(/configured/i);
    expect(status).toHaveTextContent("sha256:ab12cd34");
  });

  it("gives a secret the SERVER's refusal, not a local paraphrase of it", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-channels"));
    const badge = await screen.findByTestId("config-readonly-ROBOTHOR_TELEGRAM_BOT_TOKEN");
    expect(badge).toHaveTextContent(SECRET_REFUSAL);
    // Our own line, which is NOT a restatement of the API: `configured` reads
    // the environment and config.yaml only, so a vaulted credential is not
    // "missing".
    expect(screen.getByTestId("config-field-ROBOTHOR_TELEGRAM_BOT_TOKEN")).toHaveTextContent(
      /vault/i
    );
  });

  it("caps an over-long fingerprint rather than rendering whatever came back", async () => {
    const long = `sha256:${"a".repeat(80)}`;
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/schema")) return { ok: true, status: 200, json: async () => SCHEMA } as Response;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ...VALUES,
          values: {
            ...VALUES.values,
            ROBOTHOR_TELEGRAM_BOT_TOKEN: {
              ...VALUES.values.ROBOTHOR_TELEGRAM_BOT_TOKEN,
              value: { configured: true, fingerprint: long },
            },
          },
        }),
      } as Response;
    }) as typeof fetch);
    render(<ConfigPage visible />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-channels"));
    const status = await screen.findByTestId("config-secret-ROBOTHOR_TELEGRAM_BOT_TOKEN");
    expect(status.textContent ?? "").not.toContain(long);
    expect(status).toHaveTextContent("sha256:");
  });

  it("makes a field the environment supplies read-only, in the SERVER's words", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    await openEngine();
    const row = screen.getByTestId("config-field-ROBOTHOR_ENGINE_HOST");
    expect(within(row).queryByRole("textbox")).not.toBeInTheDocument();
    const badge = screen.getByTestId("config-readonly-ROBOTHOR_ENGINE_HOST");
    // Verbatim, including the variable the route resolved — which on a
    // mid-migration box is a deprecated alias this page cannot compute.
    expect(badge).toHaveTextContent(ENV_REFUSAL);
    expect(badge).toHaveTextContent("ROBOTHOR_OLD_ENGINE_HOST");
    // The current value is still worth reading — it is what the engine runs.
    expect(row).toHaveTextContent("0.0.0.0");
  });

  it("names no variable of its own when an older bridge sends no reason", async () => {
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/schema")) return { ok: true, status: 200, json: async () => SCHEMA } as Response;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ...VALUES,
          values: {
            ...VALUES.values,
            ROBOTHOR_ENGINE_HOST: { value: "0.0.0.0", source: "env", editable: false },
          },
        }),
      } as Response;
    }) as typeof fetch);
    render(<ConfigPage visible />);
    await openEngine();
    const badge = await screen.findByTestId("config-readonly-ROBOTHOR_ENGINE_HOST");
    expect(badge).toHaveTextContent(/did not say why/i);
    expect(badge).toHaveTextContent("genus config explain");
    // Guessing a variable name is the whole defect: a wrong one sends the
    // operator to clear something that is not set.
    expect(badge).not.toHaveTextContent("Clear the variable");
    expect(within(screen.getByTestId("config-field-ROBOTHOR_ENGINE_HOST")).queryByRole("textbox"))
      .not.toBeInTheDocument();
  });

  it("renders a field the values map omits as read-only, never as a live box", async () => {
    // The contract says GET returns all 371, so this is defensive — but the
    // defence must not be an input whose Save silently discards what was typed.
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/schema")) return { ok: true, status: 200, json: async () => SCHEMA } as Response;
      const values = { ...VALUES.values } as Record<string, unknown>;
      delete values.ROBOTHOR_LOG_DIR;
      return { ok: true, status: 200, json: async () => ({ ...VALUES, values }) } as Response;
    }) as typeof fetch);
    render(<ConfigPage visible />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-engine"));
    const row = await screen.findByTestId("config-field-ROBOTHOR_LOG_DIR");
    expect(within(row).queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("config-readonly-ROBOTHOR_LOG_DIR")).toHaveTextContent(
      /not reported by the server/i
    );
  });

  it("sends a governed field to the Flags page rather than editing it here", async () => {
    mockBridge();
    const onOpenFlags = vi.fn();
    render(<ConfigPage visible onOpenFlags={onOpenFlags} />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-flags"));
    fireEvent.click(await screen.findByTestId("config-flags-link-ROBOTHOR_RBAC_MODE"));
    expect(onOpenFlags).toHaveBeenCalledOnce();
  });

  it("keeps the link to the verdict on a governed field that is not editable", async () => {
    // Governed is always editable under the current contract, so this is a
    // latent case — but the verdict link living inside the editable branch is
    // how it would stop being latent without anybody noticing.
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/schema")) return { ok: true, status: 200, json: async () => SCHEMA } as Response;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ...VALUES,
          values: {
            ...VALUES.values,
            ROBOTHOR_RBAC_MODE: {
              value: "enforce",
              source: "env",
              editable: false,
              reason: "a future contract refused this",
            },
          },
        }),
      } as Response;
    }) as typeof fetch);
    const onOpenFlags = vi.fn();
    render(<ConfigPage visible onOpenFlags={onOpenFlags} />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-flags"));
    fireEvent.click(await screen.findByTestId("config-flags-link-ROBOTHOR_RBAC_MODE"));
    expect(onOpenFlags).toHaveBeenCalledOnce();
  });

  it("shows what each field's declared default is", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    await openEngine();
    expect(screen.getByTestId("config-default-ROBOTHOR_MAX_CONCURRENT_AGENTS")).toHaveTextContent(
      "3"
    );
  });

  it("refuses to edit a type this build does not know, rather than posting a string", async () => {
    // The settings model declares only str/int/float/bool today. A list field
    // added later would reach a text box whose save is a guaranteed 422; a
    // read-only row saying so is the honest rendering of "not supported yet".
    const listSchema = {
      groups: [
        {
          id: "engine",
          label: "Engine",
          fields: [
            {
              ...SCHEMA.groups[0].fields[1],
              name: "ROBOTHOR_FUTURE_LIST",
              env: "ROBOTHOR_FUTURE_LIST",
              type: "list",
            },
          ],
        },
      ],
    };
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/schema")) return { ok: true, status: 200, json: async () => listSchema } as Response;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          values: {
            ROBOTHOR_FUTURE_LIST: { value: "a,b", source: "config", editable: true, reason: null },
          },
          pending_restart: [],
        }),
      } as Response;
    }) as typeof fetch);
    render(<ConfigPage visible />);
    fireEvent.click(await screen.findByTestId("config-group-toggle-engine"));
    const row = await screen.findByTestId("config-field-ROBOTHOR_FUTURE_LIST");
    expect(within(row).queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("config-readonly-ROBOTHOR_FUTURE_LIST")).toHaveTextContent(
      /does not know how to edit/i
    );
  });

  it("posts ONLY the fields that changed", async () => {
    const recorded = mockBridge(() => ok(["ROBOTHOR_LOG_DIR", "ROBOTHOR_MAX_CONCURRENT_AGENTS"]));
    render(<ConfigPage visible />);
    await openEngine();

    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "/var/log/genus" },
    });
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByTestId("config-save-engine"));

    await waitFor(() => expect(recorded.patches).toHaveLength(1));
    // Typed, and only these two: the route is all-or-nothing, so a batch that
    // carried the untouched fields would let an unrelated bad value block it.
    expect(recorded.patches[0].changes).toEqual({
      ROBOTHOR_LOG_DIR: "/var/log/genus",
      ROBOTHOR_MAX_CONCURRENT_AGENTS: 7,
    });
  });

  it("will not save a section with nothing dirty in it", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    await openEngine();
    expect(screen.getByTestId("config-save-engine")).toBeDisabled();
  });

  it("lands every error of a 422 on its own field, and marks nothing saved", async () => {
    const recorded = mockBridge(() =>
      refused([
        {
          name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
          message: "ROBOTHOR_MAX_CONCURRENT_AGENTS: 'nine' is not a valid value (int parsing)",
        },
        { name: "ROBOTHOR_LOG_DIR", message: "ROBOTHOR_LOG_DIR: '' is not a valid value" },
      ])
    );
    render(<ConfigPage visible />);
    await openEngine();

    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS"), {
      target: { value: "nine" },
    });
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByTestId("config-save-engine"));

    await waitFor(() => expect(recorded.patches).toHaveLength(1));
    expect(await screen.findByTestId("config-error-ROBOTHOR_MAX_CONCURRENT_AGENTS")).toHaveTextContent(
      "'nine' is not a valid value"
    );
    expect(screen.getByTestId("config-error-ROBOTHOR_LOG_DIR")).toBeInTheDocument();
    // Nothing was written, so nothing may say it was.
    expect(screen.queryByTestId("config-saved-ROBOTHOR_LOG_DIR")).not.toBeInTheDocument();
    expect(screen.queryByTestId("config-saved-ROBOTHOR_MAX_CONCURRENT_AGENTS")).not.toBeInTheDocument();
    expect(screen.queryByTestId("config-restart-banner")).not.toBeInTheDocument();
    // And the edit is still in the box, to be corrected rather than retyped.
    expect(screen.getByTestId("config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS")).toHaveValue(null);
  });

  it("marks what a partial 500 DID write, banners it, and lands the rest", async () => {
    // The contract: a failure DURING application cannot be rolled back across a
    // file and a table, so it is 500 with `applied` naming exactly what landed.
    // Reporting that as a total failure is how an operator walks away without
    // restarting a unit a change that DID land needs.
    mockBridge(() => ({
      status: 500,
      body: {
        applied: ["ROBOTHOR_LOG_DIR"],
        pending_restart: ["robothor-engine"],
        errors: [
          {
            name: "ROBOTHOR_MAX_CONCURRENT_AGENTS",
            message: "ROBOTHOR_MAX_CONCURRENT_AGENTS: the database went away",
          },
        ],
      },
    }));
    render(<ConfigPage visible />);
    await openEngine();
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "/var/log/genus" },
    });
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_MAX_CONCURRENT_AGENTS"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByTestId("config-save-engine"));

    expect(await screen.findByTestId("config-saved-ROBOTHOR_LOG_DIR")).toBeInTheDocument();
    expect(await screen.findByTestId("config-restart-banner")).toHaveTextContent("robothor-engine");
    expect(
      await screen.findByTestId("config-error-ROBOTHOR_MAX_CONCURRENT_AGENTS")
    ).toHaveTextContent("the database went away");
    expect(screen.getByTestId("config-save-error-engine")).toHaveTextContent(/part of this section/i);
    // And the row that landed now reads what was written.
    expect(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR")).toHaveValue("/var/log/genus");
  });

  it("keeps a section with an unsaved edit visible through a filter that excludes it", async () => {
    // A dirty edit that vanishes is worse than one that is refused: the draft
    // survives, and is silently re-included in the next save of that group.
    mockBridge();
    render(<ConfigPage visible />);
    await openEngine();
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "/var/log/genus" },
    });
    fireEvent.change(screen.getByTestId("config-search"), { target: { value: "telegram" } });

    expect(await screen.findByTestId("config-group-engine")).toBeInTheDocument();
    expect(screen.getByTestId("config-hidden-changes-engine")).toHaveTextContent(/filter/i);
    expect(screen.getByTestId("config-save-engine")).toBeEnabled();
  });

  it("shows a 422 error that belongs to no rendered field rather than swallowing it", async () => {
    mockBridge(() =>
      refused([{ name: "ROBOTHOR_GONE", message: "ROBOTHOR_GONE: no such setting." }])
    );
    render(<ConfigPage visible />);
    await openEngine();
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "/var/log/genus" },
    });
    fireEvent.click(screen.getByTestId("config-save-engine"));
    expect(await screen.findByTestId("config-save-error-engine")).toHaveTextContent("no such setting");
  });

  it("marks what a 200 applied, and banners the units it named", async () => {
    mockBridge(() => ok(["ROBOTHOR_LOG_DIR"], ["robothor-engine"]));
    render(<ConfigPage visible />);
    await openEngine();
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "/var/log/genus" },
    });
    fireEvent.click(screen.getByTestId("config-save-engine"));

    expect(await screen.findByTestId("config-saved-ROBOTHOR_LOG_DIR")).toBeInTheDocument();
    const banner = await screen.findByTestId("config-restart-banner");
    expect(banner).toHaveTextContent("robothor-engine");
    // The saved value is now the current one, so the section is clean again.
    await waitFor(() => expect(screen.getByTestId("config-save-engine")).toBeDisabled());
  });

  it("shows no restart banner for a save that needs no restart", async () => {
    mockBridge(() => ok(["ROBOTHOR_DETECTORS_ENABLED"], []));
    render(<ConfigPage visible />);
    await openEngine();
    fireEvent.click(screen.getByTestId("config-switch-ROBOTHOR_DETECTORS_ENABLED"));
    fireEvent.click(screen.getByTestId("config-save-engine"));
    expect(await screen.findByTestId("config-saved-ROBOTHOR_DETECTORS_ENABLED")).toBeInTheDocument();
    expect(screen.queryByTestId("config-restart-banner")).not.toBeInTheDocument();
  });

  it("keeps the restart banner across a view switch, and lets it be dismissed", async () => {
    mockBridge(() => ok(["ROBOTHOR_LOG_DIR"], ["robothor-bridge", "robothor-engine"]));
    const view = render(<ConfigPage visible />);
    await openEngine();
    fireEvent.change(screen.getByTestId("config-input-ROBOTHOR_LOG_DIR"), {
      target: { value: "/var/log/genus" },
    });
    fireEvent.click(screen.getByTestId("config-save-engine"));
    await screen.findByTestId("config-restart-banner");

    // Settings unmounts an inactive page; the banner is not its state.
    view.unmount();
    render(<ConfigPage visible />);
    const banner = await screen.findByTestId("config-restart-banner");
    expect(banner).toHaveTextContent("robothor-bridge");
    expect(banner).toHaveTextContent("robothor-engine");

    fireEvent.click(screen.getByTestId("config-restart-dismiss"));
    await waitFor(() =>
      expect(screen.queryByTestId("config-restart-banner")).not.toBeInTheDocument()
    );
  });

  it("filters by name and by description, and opens the groups that matched", async () => {
    mockBridge();
    render(<ConfigPage visible />);
    await screen.findByTestId("config-group-engine");

    fireEvent.change(screen.getByTestId("config-search"), { target: { value: "concurrent" } });
    expect(await screen.findByTestId("config-field-ROBOTHOR_MAX_CONCURRENT_AGENTS")).toBeInTheDocument();
    expect(screen.queryByTestId("config-field-ROBOTHOR_LOG_DIR")).not.toBeInTheDocument();
    expect(screen.queryByTestId("config-group-channels")).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId("config-search"), { target: { value: "writes its logs" } });
    expect(await screen.findByTestId("config-field-ROBOTHOR_LOG_DIR")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("config-search"), { target: { value: "zzz" } });
    expect(await screen.findByTestId("config-no-matches")).toBeInTheDocument();
  });

  it("says the bridge could not be reached rather than rendering an empty form", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new Error("down"));
    render(<ConfigPage visible />);
    expect(await screen.findByTestId("config-error")).toHaveTextContent(/could not reach the bridge/i);
  });

  it("reports a refusal in the bridge's own words", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ detail: "operator only" }),
    } as Response);
    render(<ConfigPage visible />);
    expect(await screen.findByTestId("config-error")).toHaveTextContent("operator only");
  });

  it("says so when the instance declares no settings at all", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ groups: [], values: {}, pending_restart: [] }),
    } as Response);
    render(<ConfigPage visible />);
    expect(await screen.findByTestId("config-empty")).toBeInTheDocument();
  });

  it("touches nothing while it is not the page on screen", () => {
    mockBridge();
    render(<ConfigPage visible={false} />);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("reports config.yaml being ignored right now separately from a save", async () => {
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      return {
        ok: true,
        status: 200,
        json: async () =>
          url.includes("/schema")
            ? SCHEMA
            : { ...VALUES, pending_restart: ["robothor-engine"] },
      } as Response;
    }) as typeof fetch);
    render(<ConfigPage visible />);
    const notice = await screen.findByTestId("config-ignored-notice");
    expect(notice).toHaveTextContent("robothor-engine");
    expect(notice).toHaveTextContent(/environment/i);
    // It is NOT the post-save banner: nothing has been saved.
    expect(screen.queryByTestId("config-restart-banner")).not.toBeInTheDocument();
  });
});
