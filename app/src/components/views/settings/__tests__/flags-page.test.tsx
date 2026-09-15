/**
 * Settings › Flags — the governed flags, what each one is actually doing, and
 * the one write on this screen that takes effect without a restart.
 *
 * This page replaces `views/controls-view.tsx`. Every claim that view's tests
 * made is made here as well, because they were the right claims:
 *
 * * an INERT / BLIND / UNKNOWN verdict renders as a WARNING, never as healthy —
 *   zero evidence is a question, not a checkmark;
 * * only ENFORCING renders affirmatively;
 * * a change needs a reason before it can be applied (the bridge's
 *   `FlagPatch` requires one, and it is what `feature_flag_audit.reason` holds);
 * * a non-operator sees the verdict and no way to write.
 *
 * What is new is that the page is driven by the SCHEMA — `governed: true` in
 * `GET /api/settings/schema` — rather than by whatever `/api/controls`
 * happened to list, so a governed flag with no verdict yet is rendered as a
 * flag with no verdict rather than as a flag that does not exist.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FlagsPage } from "../flags-page";

function governed(name: string, choices: string[], description: string) {
  return {
    name,
    env: name,
    field: `flags.${name.toLowerCase()}`,
    group: "flags",
    aliases: [],
    type: choices.includes("true") ? "bool" : "str",
    description,
    default: choices[0],
    secret: false,
    governed: true,
    restart_required: false,
    restart_units: ["robothor-engine"],
    since: "legacy",
    hot: true,
    enum: choices,
  };
}

const LADDER = ["off", "observe", "alert", "enforce"];
const BOOL = ["true", "false"];

const SCHEMA = {
  groups: [
    {
      id: "engine",
      label: "Engine",
      fields: [
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
      ],
    },
    {
      id: "flags",
      label: "Flags",
      fields: [
        governed("ROBOTHOR_RBAC_MODE", LADDER, "Role checks on every tool call."),
        governed("ROBOTHOR_APPROVAL_MODE", LADDER, "Human approval for irreversible tools."),
        governed("ROBOTHOR_RIP_1_ENABLED", BOOL, "The first engine upgrade."),
        governed("ROBOTHOR_JUDGE_ENABLED", BOOL, "Grade a run after it finishes."),
        governed("ROBOTHOR_DNC_MODE", ["observe", "enforce"], "Do-not-contact."),
      ],
    },
  ],
};

const VALUES = {
  values: {
    ROBOTHOR_LOG_DIR: { value: "/var/log/robothor", source: "config", editable: true },
    ROBOTHOR_RBAC_MODE: { value: "enforce", source: "db", editable: true },
    ROBOTHOR_APPROVAL_MODE: { value: "observe", source: "default", editable: true },
    ROBOTHOR_RIP_1_ENABLED: { value: "false", source: "default", editable: true },
    ROBOTHOR_JUDGE_ENABLED: { value: "true", source: "env", editable: false },
    ROBOTHOR_DNC_MODE: { value: "enforce", source: "db", editable: true },
  },
  pending_restart: [],
};

function control(name: string, value: string, valid: string[], status: string, message: string) {
  return {
    name,
    value,
    valid_values: valid,
    verdict: {
      status,
      message,
      last_fired: status === "ENFORCING" ? "2026-07-14T09:00:00Z" : null,
      count_7d: status === "ENFORCING" ? 12 : 0,
    },
  };
}

const CONTROLS = [
  control("ROBOTHOR_RBAC_MODE", "enforce", LADDER, "ENFORCING", "last fired 2026-07-14 (12 / 7d)"),
  control(
    "ROBOTHOR_APPROVAL_MODE",
    "observe",
    LADDER,
    "INERT",
    "NEVER FIRED — this control cannot protect you."
  ),
  control("ROBOTHOR_RIP_1_ENABLED", "false", BOOL, "UNPROVEN", "disabled"),
  control("ROBOTHOR_JUDGE_ENABLED", "true", BOOL, "BLIND", "no evidence table for this flag"),
  // ROBOTHOR_DNC_MODE is deliberately absent: the schema governs it, the
  // controls route has not listed it. The page must still render it.
];

interface Recorded {
  patches: Array<{ url: string; body: Record<string, unknown> }>;
  controlReads: number;
}

function mockBridge(
  patchReply: () => { status: number; body: unknown } = () => ({
    status: 200,
    body: { name: "x", value: "y" },
  }),
  controlsAfterPatch: unknown[] = CONTROLS
): Recorded {
  const recorded: Recorded = { patches: [], controlReads: 0 };
  vi.spyOn(global, "fetch").mockImplementation((async (
    input: RequestInfo | URL,
    init?: RequestInit
  ) => {
    const url = String(input);
    if (init?.method === "PATCH") {
      recorded.patches.push({
        url,
        body: JSON.parse(String(init.body)) as Record<string, unknown>,
      });
      const reply = patchReply();
      return { ok: reply.status < 400, status: reply.status, json: async () => reply.body } as Response;
    }
    if (url.includes("/api/controls")) {
      const body = recorded.patches.length ? controlsAfterPatch : CONTROLS;
      recorded.controlReads += 1;
      return { ok: true, status: 200, json: async () => body } as Response;
    }
    const body = url.includes("/schema") ? SCHEMA : VALUES;
    return { ok: true, status: 200, json: async () => body } as Response;
  }) as typeof fetch);
  return recorded;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Settings › Flags", () => {
  it("renders every governed flag from the schema, and nothing else", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    for (const name of [
      "ROBOTHOR_RBAC_MODE",
      "ROBOTHOR_APPROVAL_MODE",
      "ROBOTHOR_RIP_1_ENABLED",
      "ROBOTHOR_JUDGE_ENABLED",
      "ROBOTHOR_DNC_MODE",
    ]) {
      expect(await screen.findByTestId(`flag-${name}`)).toBeInTheDocument();
    }
    // Not governed — it belongs to the Config page, and writing it here would
    // go to the flag store, which nothing reads it from.
    expect(screen.queryByTestId("flag-ROBOTHOR_LOG_DIR")).not.toBeInTheDocument();
  });

  it("groups them under Guardrails, Engine ladders and Tuning", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    const guardrails = await screen.findByTestId("flags-section-guardrails");
    expect(within(guardrails).getByTestId("flag-ROBOTHOR_RBAC_MODE")).toBeInTheDocument();
    expect(
      within(await screen.findByTestId("flags-section-ladders")).getByTestId(
        "flag-ROBOTHOR_RIP_1_ENABLED"
      )
    ).toBeInTheDocument();
    expect(
      within(await screen.findByTestId("flags-section-tuning")).getByTestId(
        "flag-ROBOTHOR_JUDGE_ENABLED"
      )
    ).toBeInTheDocument();
  });

  it("renders a flag's description and the layer its value came from", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    const row = await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    expect(row).toHaveTextContent("Role checks on every tool call.");
    expect(screen.getByTestId("flag-source-ROBOTHOR_RBAC_MODE")).toHaveTextContent("flag store");
    expect(screen.getByTestId("flag-source-ROBOTHOR_APPROVAL_MODE")).toHaveTextContent("default");
  });

  it("offers exactly the rungs the write path accepts, marking the current one", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    for (const rung of LADDER) {
      expect(screen.getByTestId(`flag-value-ROBOTHOR_RBAC_MODE-${rung}`)).toBeInTheDocument();
    }
    expect(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-enforce")).toHaveAttribute(
      "aria-pressed",
      "true"
    );
    expect(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-off")).toHaveAttribute(
      "aria-pressed",
      "false"
    );
  });

  it("renders an INERT verdict as a warning, never as healthy", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    const badge = await screen.findByTestId("flag-verdict-ROBOTHOR_APPROVAL_MODE");
    expect(badge).toHaveAttribute("data-status", "INERT");
    expect(badge.className).toMatch(/warning/);
    expect(badge.className).not.toMatch(/success/);
    expect(screen.getByTestId("flag-ROBOTHOR_APPROVAL_MODE")).toHaveTextContent(/NEVER FIRED/i);
  });

  it("renders an ENFORCING verdict affirmatively, with its evidence", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    const badge = await screen.findByTestId("flag-verdict-ROBOTHOR_RBAC_MODE");
    expect(badge).toHaveAttribute("data-status", "ENFORCING");
    expect(badge.className).toMatch(/success/);
    expect(screen.getByTestId("flag-ROBOTHOR_RBAC_MODE")).toHaveTextContent("12");
  });

  it("says a governed flag has no verdict rather than pretending it is fine", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    const badge = await screen.findByTestId("flag-verdict-ROBOTHOR_DNC_MODE");
    expect(badge).toHaveAttribute("data-status", "UNKNOWN");
    expect(badge.className).not.toMatch(/success/);
    // Still writable: the schema bounds it, so the rungs are known.
    expect(screen.getByTestId("flag-value-ROBOTHOR_DNC_MODE-enforce")).toBeInTheDocument();
  });

  it("will not apply a change without a reason", async () => {
    const recorded = mockBridge();
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    fireEvent.click(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-observe"));
    expect(screen.getByTestId("flag-apply-ROBOTHOR_RBAC_MODE")).toBeDisabled();
    expect(recorded.patches).toHaveLength(0);
  });

  it("will not apply a change that changes nothing", async () => {
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    fireEvent.change(screen.getByTestId("flag-reason-ROBOTHOR_RBAC_MODE"), {
      target: { value: "no change" },
    });
    expect(screen.getByTestId("flag-apply-ROBOTHOR_RBAC_MODE")).toBeDisabled();
  });

  it("writes through the controls route and re-reads the verdict", async () => {
    const after = [
      control("ROBOTHOR_RBAC_MODE", "observe", LADDER, "UNPROVEN", "observing; nothing blocked yet"),
      ...CONTROLS.slice(1),
    ];
    const recorded = mockBridge(() => ({ status: 200, body: { name: "x", value: "y" } }), after);
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");

    fireEvent.click(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-observe"));
    fireEvent.change(screen.getByTestId("flag-reason-ROBOTHOR_RBAC_MODE"), {
      target: { value: "soak over, stepping back to observe" },
    });
    fireEvent.click(screen.getByTestId("flag-apply-ROBOTHOR_RBAC_MODE"));

    await waitFor(() => expect(recorded.patches).toHaveLength(1));
    expect(recorded.patches[0].url).toContain("/api/controls/ROBOTHOR_RBAC_MODE");
    expect(recorded.patches[0].body).toEqual({
      value: "observe",
      reason: "soak over, stepping back to observe",
    });

    // The verdict is what the control is DOING, so it is re-read rather than
    // assumed: a flag moved to observe stops enforcing immediately.
    await waitFor(() =>
      expect(screen.getByTestId("flag-verdict-ROBOTHOR_RBAC_MODE")).toHaveAttribute(
        "data-status",
        "UNPROVEN"
      )
    );
    expect(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-observe")).toHaveAttribute(
      "aria-pressed",
      "true"
    );
  });

  it("never posts to the settings route — a flag write is hot and audited elsewhere", async () => {
    const recorded = mockBridge();
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    fireEvent.click(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-off"));
    fireEvent.change(screen.getByTestId("flag-reason-ROBOTHOR_RBAC_MODE"), {
      target: { value: "break glass" },
    });
    fireEvent.click(screen.getByTestId("flag-apply-ROBOTHOR_RBAC_MODE"));
    await waitFor(() => expect(recorded.patches).toHaveLength(1));
    expect(recorded.patches.every((p) => !p.url.endsWith("/api/settings"))).toBe(true);
  });

  it("prints a refused write in the bridge's own words", async () => {
    mockBridge(() => ({ status: 422, body: { detail: "invalid value" } }));
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    fireEvent.click(screen.getByTestId("flag-value-ROBOTHOR_RBAC_MODE-off"));
    fireEvent.change(screen.getByTestId("flag-reason-ROBOTHOR_RBAC_MODE"), {
      target: { value: "testing" },
    });
    fireEvent.click(screen.getByTestId("flag-apply-ROBOTHOR_RBAC_MODE"));
    expect(await screen.findByTestId("flag-error-ROBOTHOR_RBAC_MODE")).toHaveTextContent(
      "invalid value"
    );
  });

  it("keeps a flag the environment supplies writable, and says what a write would do", async () => {
    // `robothor.flags.store.resolve` is DB row -> environment -> default, so an
    // operator row here OUTRANKS the variable and the write is not futile. The
    // settings API reports `editable: false` for it anyway (it applies the
    // non-governed env rule), and following that would disable a control that
    // works — the inverse of an inert control, and just as dishonest.
    mockBridge();
    render(<FlagsPage visible role="owner" />);
    await screen.findByTestId("flag-ROBOTHOR_JUDGE_ENABLED");
    expect(screen.getByTestId("flag-value-ROBOTHOR_JUDGE_ENABLED-false")).toBeEnabled();
    const note = screen.getByTestId("flag-env-note-ROBOTHOR_JUDGE_ENABLED");
    expect(note).toHaveTextContent(/environment/i);
    expect(note).toHaveTextContent(/outrank/i);
  });

  it("shows a non-operator the verdicts and no way to write", async () => {
    mockBridge();
    render(<FlagsPage visible role="viewer" />);
    await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE");
    expect(screen.queryByTestId("flag-value-ROBOTHOR_RBAC_MODE-off")).not.toBeInTheDocument();
    expect(screen.queryByTestId("flag-reason-ROBOTHOR_RBAC_MODE")).not.toBeInTheDocument();
    expect(screen.queryByTestId("flag-apply-ROBOTHOR_RBAC_MODE")).not.toBeInTheDocument();
    expect(screen.getByTestId("flag-readonly-ROBOTHOR_RBAC_MODE")).toHaveTextContent(/operator/i);
    // Reading what the flag is doing stays available.
    expect(screen.getByTestId("flag-verdict-ROBOTHOR_RBAC_MODE")).toHaveAttribute(
      "data-status",
      "ENFORCING"
    );
  });

  it("says the bridge could not be reached rather than rendering an empty page", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new Error("down"));
    render(<FlagsPage visible role="owner" />);
    expect(await screen.findByTestId("flags-error")).toHaveTextContent(/could not reach the bridge/i);
  });

  it("renders the flags even when the verdicts cannot be read", async () => {
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/controls")) {
        return { ok: false, status: 500, json: async () => ({ detail: "evidence table gone" }) } as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => (url.includes("/schema") ? SCHEMA : VALUES),
      } as Response;
    }) as typeof fetch);
    render(<FlagsPage visible role="owner" />);
    expect(await screen.findByTestId("flag-ROBOTHOR_RBAC_MODE")).toBeInTheDocument();
    expect(screen.getByTestId("flags-verdict-error")).toHaveTextContent("evidence table gone");
    expect(screen.getByTestId("flag-verdict-ROBOTHOR_RBAC_MODE")).toHaveAttribute(
      "data-status",
      "UNKNOWN"
    );
  });

  it("says so when the instance governs nothing", async () => {
    vi.spyOn(global, "fetch").mockImplementation((async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/controls")) return { ok: true, status: 200, json: async () => [] } as Response;
      return {
        ok: true,
        status: 200,
        json: async () =>
          url.includes("/schema") ? { groups: [SCHEMA.groups[0]] } : { values: {}, pending_restart: [] },
      } as Response;
    }) as typeof fetch);
    render(<FlagsPage visible role="owner" />);
    expect(await screen.findByTestId("flags-empty")).toBeInTheDocument();
  });

  it("touches nothing while it is not the page on screen", () => {
    mockBridge();
    render(<FlagsPage visible={false} role="owner" />);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
