/**
 * Settings › Plugins — what is installed, what the lockfile says about it, and
 * the three acts an operator has: record, toggle, reload.
 *
 * The claims worth pinning, in the order the page makes them:
 *
 * * **`verdict` is never rendered.** The lock row carries `"unscanned"`, and
 *   nothing scans a plugin yet. A screen that printed it would be making a
 *   safety claim out of a placeholder, on the one screen where third-party
 *   code is turned on.
 * * **A fresh install's primary affordance is Record.** Until a sync has run
 *   the lockfile does not exist, every row is unrecorded, and enable/disable
 *   both answer 404 — so without that button the page is read-only until
 *   somebody gets a shell on the box.
 * * **409 and 503 are different sentences.** 409 means the file's contents
 *   cannot be read and the escape is `genus plugin sync --force` on the CLI
 *   (deliberately not over HTTP); 503 means the disk will not take the write,
 *   and forcing cannot fix a filesystem.
 * * **A toggle is not an apply.** enable/disable answer `reloaded: false`; the
 *   running engine keeps serving the set it discovered. The row's switch moves,
 *   the STATE pill does not, and a bar appears saying so.
 * * **`disabled by operator` is not a failure.** It is the operator's own
 *   decision arriving back at them in the reload report, and it is rendered
 *   apart from a real refusal.
 * * **A refusal is filed by `failures[].distribution`, never by a guess.**
 *   The page used to weigh the group, `enabled`, a squashed name match and
 *   `manifest.declared` (which holds CONTRIBUTION names, a different
 *   namespace). The engine now answers the question; `null` is "unattributed"
 *   and nothing else may fill it in.
 * * **`lockfile.problem` is the engine's own sentence.** `malformed` covers
 *   four faults with four remedies, and the page's single sentence for all of
 *   them was one remedy short.
 * * **`review` is the normal install path, not an error state.** The verdict
 *   pill is a word — never a checkmark — Install is dead until the verdict is
 *   `safe` or the operator has ticked "I accept the review findings", and a
 *   `blocked` plan says out loud that it will not be installed.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PluginsPage } from "../plugins-page";

const LOADED = {
  name: "genus-hostinfo",
  version: "0.1.0",
  enabled: true,
  recorded: true,
  verdict: "unscanned",
  state: "loaded",
  drifted: false,
  groups: ["genus.schemas", "genus.services", "genus.tools"],
  contributions: { tools: 1, schemas: 1, services: 1 },
  failure_reason: null,
  /*
    The real `plugins/genus-hostinfo`, verbatim: the ENTRY POINT is `hostinfo`
    (`[project.entry-points."genus.tools"] hostinfo = …`) and the manifest
    declares `host_state`, the HANDLER it contributes. `loader.py` compares
    `declared` against the payload's keys, so these are two different
    namespaces — and `failures[].name` is an entry-point name. A fixture where
    the two coincide (which the B15a contract sample also has) makes an
    attribution rule that cannot work look like one that does.
  */
  manifest: { contract_version: 1, declared: { handlers: ["host_state"] } },
  // Recorded by `genus plugin sync` over something somebody pip-installed:
  // no source, so `remove` refuses it and the page must not offer one.
  source: null,
};

/** What `genus plugin install` put there — the only rows Remove may act on. */
const FROM_REGISTRY = {
  name: "genus-weather",
  version: "1.4.0",
  enabled: true,
  recorded: true,
  verdict: "review",
  state: "loaded",
  drifted: false,
  groups: ["genus.tools"],
  contributions: { tools: 2 },
  failure_reason: null,
  manifest: { contract_version: 1, declared: { handlers: ["weather_now"] } },
  source: {
    origin: "registry",
    installed_at: "2026-09-15T10:00:00+00:00",
    index_url: "https://plugins.example.org/index.json",
    publisher_key_id: "genus-2026",
  },
};

const OFF = {
  name: "genus-notes",
  version: "2.1.0",
  enabled: false,
  recorded: true,
  verdict: "unscanned",
  state: "disabled",
  drifted: false,
  groups: ["genus.jobs"],
  contributions: {},
  failure_reason: "disabled by operator",
  manifest: { contract_version: 1, declared: { jobs: ["nightly_notes"] } },
};

const DRIFTED = {
  name: "genus-widgets",
  version: "0.4.2",
  enabled: true,
  recorded: true,
  verdict: "unscanned",
  state: "failed",
  drifted: true,
  groups: ["genus.tools"],
  contributions: {},
  failure_reason: "manifest changed since it was recorded; run genus plugin sync",
  manifest: { contract_version: 1, declared: { handlers: ["widget_list"] } },
};

/** Installed, never recorded: `verdict` is `""` and `enabled` is `true`. */
const UNRECORDED = {
  name: "genus-ledger",
  version: "1.0.0",
  enabled: true,
  recorded: false,
  verdict: "",
  state: "loaded",
  drifted: false,
  groups: ["genus.services"],
  contributions: { services: 2 },
  failure_reason: null,
  manifest: null,
};

const INDEX = "https://plugins.example.org/index.json";

const LISTING = {
  generation: 3,
  indexes: [INDEX],
  lockfile: { path_configured: true, present: true, malformed: false, rows: 3, problem: null },
  plugins: [LOADED, OFF, DRIFTED],
};

/** A box where `genus plugin sync` has never run. */
const FRESH = {
  generation: 1,
  indexes: [INDEX],
  lockfile: { path_configured: true, present: false, malformed: false, rows: 0, problem: null },
  plugins: [{ ...UNRECORDED }, { ...LOADED, recorded: false, verdict: "" }],
};

/**
 * `POST /api/plugins/install` with `dry_run: true`. The shape is C6a's §1.3
 * verbatim, and `review` is the COMMON verdict: `safe` means "contributes
 * tools and touches nothing outside this process", so anything importing `os`,
 * or whose manifest omits `entry_points:`, lands here.
 */
const REVIEW_PLAN = {
  plan: {
    name: "genus-weather",
    version: "1.4.0",
    origin: "registry",
    index_url: INDEX,
    publisher_key_id: "genus-2026",
    filename: "genus_weather-1.4.0-py3-none-any.whl",
    sha256: "abc123def456" + "0".repeat(52),
    size: 8192,
    summary: "Weather as a tool",
    verdict: "review",
    reasons: [
      "genus_weather/__init__.py:4: imports os",
      "the manifest declares no entry_points:, so the comparison is only at group granularity",
    ],
    prompt_scan: "static-only",
    groups: ["genus.tools"],
    files_scanned: 3,
    members_accounted: 11,
    accept_review: false,
  },
  installed: false,
  dry_run: true,
  row: null,
  reload_hint: "reload the engine (SIGHUP) or restart to apply",
  note: "",
};

const INSTALLED_REPLY = {
  ...REVIEW_PLAN,
  plan: { ...REVIEW_PLAN.plan, accept_review: true },
  installed: true,
  dry_run: false,
  row: {
    name: "genus-weather",
    version: "1.4.0",
    manifest_sha256: "1".repeat(64),
    verdict: "review",
    enabled: true,
    kinds: ["genus.tools"],
    recorded_at: "2026-09-15T10:00:00+00:00",
    dist_sha256: "abc123def456" + "0".repeat(52),
    members_accounted: 11,
    source: {
      origin: "registry",
      installed_at: "2026-09-15T10:00:00+00:00",
      index_url: INDEX,
      publisher_key_id: "genus-2026",
    },
  },
};

interface Reply {
  status: number;
  body: unknown;
}

interface Recorded {
  posts: Array<{ url: string; method: string; body: Record<string, unknown> }>;
  listReads: number;
}

function parseBody(init?: RequestInit): Record<string, unknown> {
  if (typeof init?.body !== "string") return {};
  try {
    return JSON.parse(init.body) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * One bridge stand-in for every test.
 *
 * `listings` is a queue: the page re-reads `GET /api/plugins` after a sync and
 * after a reload, and "the second read answers something else" is how those
 * refreshes are proved to have happened.
 */
function mockBridge(options: {
  listings?: unknown[];
  sync?: Reply;
  toggle?: Reply;
  reload?: Reply;
  /** A queue: Preview and Install post to the same URL and answer differently. */
  install?: Reply[];
  remove?: Reply;
  listStatus?: number;
}): Recorded {
  const recorded: Recorded = { posts: [], listReads: 0 };
  const listings = options.listings ?? [LISTING];
  const installs = [...(options.install ?? [])];
  vi.spyOn(global, "fetch").mockImplementation((async (
    input: RequestInfo | URL,
    init?: RequestInit
  ) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (method === "POST") {
      recorded.posts.push({ url, method, body: parseBody(init) });
      const reply = url.endsWith("/sync")
        ? (options.sync ?? { status: 200, body: {} })
        : url.endsWith("/reload")
          ? (options.reload ?? { status: 200, body: { generation: 4, loaded: 1, failures: [] } })
          : url.endsWith("/install")
            ? (installs.shift() ?? { status: 200, body: REVIEW_PLAN })
            : url.endsWith("/remove")
              ? (options.remove ?? {
                  status: 200,
                  body: {
                    name: "genus-weather",
                    removed: true,
                    row_dropped: true,
                    reload_hint: "reload the engine (SIGHUP) or restart to apply",
                    note: "",
                  },
                })
              : (options.toggle ?? { status: 200, body: {} });
      return {
        ok: reply.status < 400,
        status: reply.status,
        json: async () => reply.body,
      } as Response;
    }
    const status = options.listStatus ?? 200;
    const index = Math.min(recorded.listReads, listings.length - 1);
    recorded.listReads += 1;
    return {
      ok: status < 400,
      status,
      json: async () => (status < 400 ? listings[index] : { detail: "refused" }),
    } as Response;
  }) as typeof fetch);
  return recorded;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Settings › Plugins — the listing", () => {
  it("renders one card per distribution, on the contract's exact shape", async () => {
    mockBridge({});
    render(<PluginsPage visible />);

    const card = await screen.findByTestId("plugin-genus-hostinfo");
    expect(card.textContent).toContain("genus-hostinfo");
    expect(card.textContent).toContain("0.1.0");
    expect(within(card).getByTestId("plugin-state-genus-hostinfo")).toHaveAttribute(
      "data-state",
      "loaded"
    );
    // Groups are the entry-point groups, in full: an operator greps for these.
    expect(within(card).getByTestId("plugin-groups-genus-hostinfo").textContent).toContain(
      "genus.tools"
    );
    // `{kind: count}` reads as "tools × 1", not as a bare number.
    expect(within(card).getByTestId("plugin-contributions-genus-hostinfo").textContent).toContain(
      "tools × 1"
    );
    const manifest = within(card).getByTestId("plugin-manifest-genus-hostinfo");
    expect(manifest.textContent).toContain("1");
    expect(manifest.textContent).toContain("host_state");

    expect(screen.getByTestId("plugins-generation").textContent).toContain("3");
    expect(screen.getByTestId("plugins-lockfile").textContent).toContain("3");
  });

  it("styles a disabled distribution as a decision and a failed one as a fault", async () => {
    mockBridge({});
    render(<PluginsPage visible />);

    const off = await screen.findByTestId("plugin-state-genus-notes");
    expect(off).toHaveAttribute("data-state", "disabled");
    expect(off.className).not.toContain("destructive");

    const failed = screen.getByTestId("plugin-state-genus-widgets");
    expect(failed).toHaveAttribute("data-state", "failed");
    expect(failed.className).toContain("destructive");
  });

  it("badges drift and carries the server's own reason sentence", async () => {
    mockBridge({});
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-widgets");
    expect(screen.getByTestId("plugin-drifted-genus-widgets")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-failure-genus-widgets").textContent).toContain(
      "manifest changed since it was recorded; run genus plugin sync"
    );
    expect(screen.queryByTestId("plugin-drifted-genus-hostinfo")).toBeNull();
  });

  it("never renders the verdict, because nothing has scanned anything", async () => {
    mockBridge({});
    render(<PluginsPage visible />);

    const page = await screen.findByTestId("settings-page-plugins");
    expect(page.textContent).not.toContain("unscanned");
    expect(page.textContent).not.toMatch(/\bverdict\b/i);
    expect(page.textContent).not.toMatch(/\bsafe\b/i);
  });
});

describe("Settings › Plugins — recording", () => {
  it("makes Record the primary affordance when the lockfile does not exist", async () => {
    const recorded = mockBridge({
      listings: [FRESH, LISTING],
      sync: {
        status: 200,
        body: {
          recorded: ["genus-hostinfo", "genus-ledger"],
          added: ["genus-hostinfo", "genus-ledger"],
          updated: [],
          removed: [],
          reloaded: false,
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-record-empty");
    fireEvent.click(screen.getByTestId("plugins-record"));

    await waitFor(() =>
      expect(recorded.posts.map((p) => p.url)).toContain("/api/bridge/api/plugins/sync")
    );
    const result = await screen.findByTestId("plugins-record-result");
    expect(result.textContent).toContain("genus-ledger");
    // Recording is not applying — the response says `reloaded: false`.
    expect(result.textContent).toMatch(/not|nothing/i);
    // …and the listing is re-read, because every row's `recorded` just changed.
    await waitFor(() => expect(recorded.listReads).toBeGreaterThan(1));
  });

  it("points a 409 at the CLI, which is the only place --force exists", async () => {
    // Verbatim shape of `robothor/plugins/lockfile.py::sync`'s refusal.
    const detail =
      "the lockfile is not valid JSON (JSONDecodeError), so which plugins you disabled cannot " +
      "be read. Rewriting it now would silently re-enable every one of them. Repair the file, " +
      "or re-run with --force to rebuild it from what is installed and accept losing those " +
      "decisions.";
    mockBridge({ listings: [LISTING], sync: { status: 409, body: { detail } } });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-record"));

    const error = await screen.findByTestId("plugins-record-error");
    expect(error.textContent).toContain(detail);
    expect(screen.getByTestId("plugins-record-force").textContent).toContain(
      "genus plugin sync --force"
    );
  });

  it("does not offer --force for a 503, which forcing cannot fix", async () => {
    const detail = "the lockfile path could not be written (IsADirectoryError)";
    mockBridge({ listings: [LISTING], sync: { status: 503, body: { detail } } });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-record"));

    const error = await screen.findByTestId("plugins-record-error");
    expect(error.textContent).toContain(detail);
    expect(screen.queryByTestId("plugins-record-force")).toBeNull();
  });

  /**
   * `malformed: true` is WHOLE-FILE damage and nothing else — the path will not
   * read, the bytes are not text, the JSON does not parse, or there is no
   * `plugins` list (`robothor/plugins/lockfile.py::read_lockfile`). Unreadable
   * ROWS leave the flag false, and this listing carries no signal for them at
   * all. `Lockfile.usable = present and not malformed`, and the loader opens
   * with `if not lock.usable: return None` — so in this state the engine is
   * ignoring the file completely and every plugin the operator turned off is
   * being imported right now.
   */
  it("says that a damaged lockfile governs NOTHING, which is the opposite of reassurance", async () => {
    mockBridge({
      listings: [
        {
          ...LISTING,
          // No `problem`: an older bridge. The page keeps its own sentence.
          lockfile: { path_configured: true, present: true, malformed: true, rows: 0 },
        },
      ],
    });
    render(<PluginsPage visible />);

    const warning = await screen.findByTestId("plugins-lockfile-malformed");
    expect(warning.textContent).toMatch(/every .*plugin .*(is|are) loading|loading again|nothing is refused/i);
    expect(warning.textContent).toMatch(/turned off|disabled/i);
    // The claim that made this a Critical: there is no per-row damage signal in
    // this payload, and in this state no row governs anything.
    expect(warning.textContent).not.toMatch(/still govern/i);
    // `malformed` also covers the unreadable PATH, where sync answers 503 and
    // --force cannot help. The card points at Record and lets the refusal say
    // which case it is.
    expect(warning.textContent).not.toContain("--force");
    expect(warning.textContent).toMatch(/record/i);
  });

  it("does not report a damaged lockfile as a count of rows it recorded", async () => {
    mockBridge({
      listings: [
        {
          ...LISTING,
          lockfile: { path_configured: true, present: true, malformed: true, rows: 0 },
        },
      ],
    });
    render(<PluginsPage visible />);

    const chip = await screen.findByTestId("plugins-lockfile");
    expect(chip.textContent).not.toMatch(/0 recorded rows/);
    expect(chip.textContent).toMatch(/unreadable|could not be read/i);
  });

  /**
   * `malformed` is one flag over four faults with four different remedies:
   * the path will not read, the bytes are not text, the JSON does not parse,
   * or there is no `plugins` list. The page's own sentence had to cover all
   * four at once, so it was one remedy short for each — it sent an operator
   * whose lockfile PATH is a directory to repair the file's contents.
   * `lockfile.problem` is the sentence the CLI and the doctor already print.
   */
  it("prints the engine's own sentence for WHICH damage the lockfile has", async () => {
    mockBridge({
      listings: [
        {
          ...LISTING,
          lockfile: {
            path_configured: true,
            present: true,
            malformed: true,
            rows: 0,
            problem: "cannot be read (IsADirectoryError)",
          },
        },
      ],
    });
    render(<PluginsPage visible />);

    const warning = await screen.findByTestId("plugins-lockfile-malformed");
    expect(warning.textContent).toContain("cannot be read (IsADirectoryError)");
    // The consequence stays: in this state the engine ignores the file whole.
    expect(warning.textContent).toMatch(/turned off|disabled/i);
    expect(warning.textContent).not.toContain("--force");
  });

  it("keeps its own sentence when an older bridge sends no problem", async () => {
    mockBridge({
      listings: [
        {
          ...LISTING,
          lockfile: { path_configured: true, present: true, malformed: true, rows: 0 },
        },
      ],
    });
    render(<PluginsPage visible />);

    const warning = await screen.findByTestId("plugins-lockfile-malformed");
    expect(warning.textContent).toMatch(/could not be read at all/i);
  });

  it("does not print a problem the engine says is null", async () => {
    mockBridge({ listings: [LISTING] });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    expect(screen.queryByTestId("plugins-lockfile-malformed")).toBeNull();
  });

  /**
   * `sync` refuses with a 409 for TWO reasons: the file cannot be read in full
   * (`--force` rebuilds it), and no lockfile path resolves at all
   * (`lockfile.py::sync` → `refused="no lockfile path resolves"`), where there
   * is no file to force and nowhere to put one. The server's own sentence is
   * what tells them apart: it names `--force` in the first case only.
   */
  it("offers the CLI escape only when the server's own refusal names it", async () => {
    mockBridge({
      listings: [LISTING],
      sync: { status: 409, body: { detail: "no lockfile path resolves" } },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-record"));

    expect((await screen.findByTestId("plugins-record-error")).textContent).toContain(
      "no lockfile path resolves"
    );
    expect(screen.queryByTestId("plugins-record-force")).toBeNull();
  });

  it("does not promise a rejected copy the platform only makes when it can", async () => {
    mockBridge({
      listings: [LISTING],
      sync: {
        status: 409,
        body: { detail: "the lockfile is not valid JSON; re-run with --force to rebuild it" },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-record"));

    // `_preserve_rejected` logs and returns "" when the copy fails, and the CLI
    // prints that line only when it succeeded.
    const hint = await screen.findByTestId("plugins-record-force");
    expect(hint.textContent).toMatch(/where it can|if it can|says so/i);
  });

  it("offers no working Record when no lockfile path resolves at all", async () => {
    mockBridge({
      listings: [
        {
          ...LISTING,
          lockfile: { path_configured: false, present: false, malformed: false, rows: 0 },
        },
      ],
    });
    render(<PluginsPage visible />);

    expect(await screen.findByTestId("plugins-lockfile-unconfigured")).toBeInTheDocument();
    // One card, not two saying different things.
    expect(screen.queryByTestId("plugins-record-empty")).toBeNull();
    expect(screen.getByTestId("plugins-record")).toBeDisabled();
  });
});

describe("Settings › Plugins — enabling and disabling", () => {
  it("refuses to offer a switch for a distribution with no lock row", async () => {
    mockBridge({ listings: [FRESH] });
    render(<PluginsPage visible />);

    const toggle = await screen.findByTestId("plugin-switch-genus-ledger");
    expect(toggle).toBeDisabled();
    expect(screen.getByTestId("plugin-hint-genus-ledger").textContent).toMatch(/record/i);
  });

  it("posts disable, takes the new value from the response, and leaves the state pill alone", async () => {
    const recorded = mockBridge({
      listings: [LISTING],
      toggle: {
        status: 200,
        body: {
          name: "genus-hostinfo",
          version: "0.1.0",
          manifest_sha256: "0".repeat(64),
          verdict: "unscanned",
          enabled: false,
          kinds: ["genus.schemas", "genus.services", "genus.tools"],
          recorded_at: "2026-09-15T00:00:00+00:00",
          reloaded: false,
        },
      },
    });
    render(<PluginsPage visible />);

    const toggle = await screen.findByTestId("plugin-switch-genus-hostinfo");
    expect(toggle).toHaveAttribute("aria-checked", "true");
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(recorded.posts.map((p) => p.url)).toContain(
        "/api/bridge/api/plugins/genus-hostinfo/disable"
      )
    );
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
    // The engine is still serving what it discovered: the pill must not lie.
    expect(screen.getByTestId("plugin-state-genus-hostinfo")).toHaveAttribute(
      "data-state",
      "loaded"
    );
    expect(screen.getByTestId("plugin-note-genus-hostinfo").textContent).toMatch(/reload/i);
  });

  it("posts enable for a distribution that is currently off", async () => {
    const recorded = mockBridge({
      listings: [LISTING],
      toggle: { status: 200, body: { name: "genus-notes", enabled: true, reloaded: false } },
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-switch-genus-notes"));
    await waitFor(() =>
      expect(recorded.posts.map((p) => p.url)).toContain(
        "/api/bridge/api/plugins/genus-notes/enable"
      )
    );
  });

  it("says the engine is unreachable when a toggle answers 502", async () => {
    mockBridge({
      listings: [LISTING],
      toggle: { status: 502, body: { error: "Bridge service unavailable" } },
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-switch-genus-notes"));
    const error = await screen.findByTestId("plugin-error-genus-notes");
    expect(error.textContent).toMatch(/engine/i);
    expect(error.textContent).toMatch(/unreachable/i);
    expect(error.textContent).not.toMatch(/502/);
  });

  it("keeps one default for a missing enabled: true, as the contract has it", async () => {
    // `normalizePlugin` already reads a missing `enabled` as true (unrecorded
    // is not off). The lock-row reader must agree, or a 200 with a field this
    // build did not expect draws an enabled plugin as switched off.
    mockBridge({
      listings: [LISTING],
      toggle: { status: 200, body: { name: "genus-notes", reloaded: false } },
    });
    render(<PluginsPage visible />);

    const toggle = await screen.findByTestId("plugin-switch-genus-notes");
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "true"));
  });

  it("lands a 404 on the row in the server's own words", async () => {
    mockBridge({
      listings: [LISTING],
      toggle: { status: 404, body: { detail: "no lock row for genus-notes" } },
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-switch-genus-notes"));
    const error = await screen.findByTestId("plugin-error-genus-notes");
    expect(error.textContent).toContain("no lock row for genus-notes");
  });
});

describe("Settings › Plugins — reloading", () => {
  it("raises the bar only once a toggle has changed something", async () => {
    mockBridge({
      listings: [LISTING],
      toggle: { status: 200, body: { name: "genus-notes", enabled: true, reloaded: false } },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-notes");
    expect(screen.queryByTestId("plugins-reload-bar")).toBeNull();

    fireEvent.click(screen.getByTestId("plugin-switch-genus-notes"));
    await screen.findByTestId("plugins-reload-bar");
  });

  it("separates a real refusal from the operator's own decision", async () => {
    const recorded = mockBridge({
      listings: [LISTING, LISTING],
      reload: {
        status: 200,
        body: {
          generation: 4,
          loaded: 2,
          failures: [
            {
              name: "nightly_notes",
              group: "genus.jobs",
              reason: "disabled by operator",
              distribution: "genus-notes",
            },
            {
              name: "widget_list",
              group: "genus.tools",
              reason: "ImportError: no module named x",
              distribution: "genus-widgets",
            },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const report = await screen.findByTestId("plugins-reload-result");
    expect(report.textContent).toContain("2");

    // Filed by `distribution`, which the engine answers. `name` is the entry
    // point and matches neither card by string equality.
    const intended = screen.getByTestId("plugins-reload-intended-genus-notes");
    expect(intended.textContent).toMatch(/intend|decision|on purpose/i);
    const fault = screen.getByTestId("plugins-reload-failure-genus-widgets");
    expect(fault.textContent).toContain("ImportError: no module named x");

    // The listing is re-read: a reload changes every row's state.
    await waitFor(() => expect(recorded.listReads).toBeGreaterThan(1));
  });

  it("retires the row notes the toggle left, once the reload they asked for has run", async () => {
    mockBridge({
      listings: [LISTING, LISTING],
      toggle: { status: 200, body: { name: "genus-notes", enabled: true, reloaded: false } },
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-switch-genus-notes"));
    await screen.findByTestId("plugin-note-genus-notes");

    fireEvent.click(screen.getByTestId("plugins-reload"));
    await screen.findByTestId("plugins-reload-result");
    // "Reload to apply it" under a row that has just been reloaded is the page
    // contradicting the report above it.
    await waitFor(() => expect(screen.queryByTestId("plugin-note-genus-notes")).toBeNull());
  });

  /**
   * The engine answers `distribution`, and nothing else is consulted.
   *
   * The page used to weigh the group, then `enabled` for a `disabled by
   * operator` refusal, then a `-`/`_`-squashed prefix match on the
   * distribution's own name, then `manifest.declared` — and `acme-nightly-notes`
   * is exactly the fixture that heuristic got wrong, because its name ends with
   * the entry-point name of a refusal that is not its own. A wrong answer here
   * reports a plugin the operator did not disable as one they did, beside a card
   * drawing that same plugin as loaded.
   */
  it("files a refusal under the distribution the engine named, not one whose name resembles it", async () => {
    const NOTES = { ...OFF, groups: ["genus.jobs"] };
    const ACME = {
      name: "acme-nightly-notes",
      version: "3.0.0",
      enabled: true,
      recorded: true,
      verdict: "unscanned",
      state: "loaded",
      drifted: false,
      groups: ["genus.jobs"],
      contributions: { jobs: 1 },
      failure_reason: null,
      manifest: null,
      source: null,
    };
    mockBridge({
      listings: [
        { ...LISTING, plugins: [NOTES, ACME] },
        { ...LISTING, plugins: [NOTES, ACME] },
      ],
      reload: {
        status: 200,
        body: {
          generation: 4,
          loaded: 1,
          failures: [
            {
              name: "nightly_notes",
              group: "genus.jobs",
              reason: "disabled by operator",
              distribution: "genus-notes",
            },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-acme-nightly-notes");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    await screen.findByTestId("plugins-reload-intended-genus-notes");
    expect(screen.queryByTestId("plugins-reload-intended-acme-nightly-notes")).toBeNull();
    expect(screen.queryByTestId("plugins-reload-failure-acme-nightly-notes")).toBeNull();
  });

  /**
   * `null` means the metadata layer could not name the distribution — which
   * `inventory()` reacts to by skipping the row entirely, while `load_plugins`
   * still loads and still fails its entry points. So a refusal with no card
   * behind it is reachable in production, and being alone in a group is not
   * evidence of having failed. The page must not fill the gap in.
   */
  it("leaves a refusal the engine could not attribute in the unattributed bucket", async () => {
    const ONLY = { ...LOADED, groups: ["genus.tools"] };
    mockBridge({
      listings: [
        { ...LISTING, plugins: [ONLY] },
        { ...LISTING, plugins: [ONLY] },
      ],
      reload: {
        status: 200,
        body: {
          generation: 5,
          loaded: 1,
          failures: [
            {
              name: "weather_now",
              group: "genus.tools",
              reason: "ImportError: No module named 'requests'",
              distribution: null,
            },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const unmatched = await screen.findByTestId("plugins-reload-unmatched");
    expect(unmatched.textContent).toContain("weather_now");
    expect(screen.queryByTestId("plugins-reload-failure-genus-hostinfo")).toBeNull();
  });

  it("treats a bridge too old to send distribution as unattributed, not as a guess", async () => {
    const ONLY = { ...LOADED, groups: ["genus.tools"] };
    mockBridge({
      listings: [
        { ...LISTING, plugins: [ONLY] },
        { ...LISTING, plugins: [ONLY] },
      ],
      reload: {
        status: 200,
        body: {
          generation: 5,
          loaded: 0,
          // No `distribution` key at all: the field is additive, and an older
          // bridge simply does not send it.
          failures: [{ name: "hostinfo", group: "genus.tools", reason: "SyntaxError: bad code" }],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    expect((await screen.findByTestId("plugins-reload-unmatched")).textContent).toContain(
      "SyntaxError: bad code"
    );
    expect(screen.queryByTestId("plugins-reload-failure-genus-hostinfo")).toBeNull();
  });

  /**
   * The shape the box actually answers with.
   *
   * `manifest.declared` holds CONTRIBUTION names — `host_state` for
   * `genus-hostinfo` — while `failures[].name` is the ENTRY-POINT name,
   * `hostinfo`. Neither is the distribution, and `distribution` is what files
   * the line.
   */
  it("attributes a real failure on a plugin whose manifest declares other names", async () => {
    const ONLY = {
      ...LOADED,
      state: "failed",
      contributions: {},
      failure_reason: "ImportError: No module named 'psutil'",
    };
    mockBridge({
      listings: [
        { ...LISTING, plugins: [ONLY] },
        { ...LISTING, plugins: [ONLY] },
      ],
      reload: {
        status: 200,
        body: {
          generation: 8,
          loaded: 0,
          failures: [
            {
              name: "hostinfo",
              group: "genus.tools",
              reason: "ImportError: No module named 'psutil'",
              distribution: "genus-hostinfo",
            },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const line = await screen.findByTestId("plugins-reload-failure-genus-hostinfo");
    expect(line.textContent).toContain("ImportError: No module named 'psutil'");
    expect(screen.queryByTestId("plugins-reload-unmatched")).toBeNull();
  });

  it("prints the entry-point name and group beside an attributed line, so it can be checked", async () => {
    mockBridge({
      listings: [LISTING, LISTING],
      reload: {
        status: 200,
        body: {
          generation: 4,
          loaded: 2,
          failures: [
            {
              name: "widget_list",
              group: "genus.tools",
              reason: "ImportError: no module named x",
              distribution: "genus-widgets",
            },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-widgets");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const line = await screen.findByTestId("plugins-reload-failure-genus-widgets");
    expect(line.textContent).toContain("widget_list");
    expect(line.textContent).toContain("genus.tools");
  });

  it("marks a mixed bucket per failure, not by whether every one of them is intended", async () => {
    /*
      One distribution, two groups, one deliberate refusal and one real fault.

      Synthetic on purpose: a clean engine refuses every entry point of a
      disabled distribution the same way, so a mixed bucket does not arise from
      it. It arises from a mis-attribution, which is exactly the case this page
      must not render as "you turned this off" in destructive colour — so the
      rendering is pinned for a payload the page can be handed rather than only
      for the one it expects.
    */
    const BOTH = {
      ...LOADED,
      enabled: false,
      state: "disabled",
      groups: ["genus.tools", "genus.services"],
      manifest: {
        contract_version: 1,
        declared: { handlers: ["host_state"], services: ["host_state"] },
      },
    };
    mockBridge({
      listings: [
        { ...LISTING, plugins: [BOTH] },
        { ...LISTING, plugins: [BOTH] },
      ],
      reload: {
        status: 200,
        body: {
          generation: 6,
          loaded: 0,
          failures: [
            {
              name: "hostinfo",
              group: "genus.tools",
              reason: "disabled by operator",
              distribution: "genus-hostinfo",
            },
            {
              name: "hostinfo",
              group: "genus.services",
              reason: "SyntaxError: bad code",
              distribution: "genus-hostinfo",
            },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const intended = await screen.findByTestId("plugins-reload-intended-genus-hostinfo");
    expect(intended.className).not.toContain("destructive");
    const fault = screen.getByTestId("plugins-reload-failure-genus-hostinfo");
    expect(fault.textContent).toContain("SyntaxError: bad code");
    expect(fault.textContent).not.toContain("disabled by operator");
  });

  it("retires a reload report when a toggle changes what it described", async () => {
    mockBridge({
      listings: [LISTING, LISTING, LISTING],
      toggle: { status: 200, body: { name: "genus-notes", enabled: true, reloaded: false } },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-notes");
    fireEvent.click(screen.getByTestId("plugins-reload"));
    await screen.findByTestId("plugins-reload-result");

    // The report describes the set the engine loaded; a toggle changes what a
    // reload would load next, so the old report is about a previous moment.
    fireEvent.click(screen.getByTestId("plugin-switch-genus-notes"));
    await waitFor(() => expect(screen.queryByTestId("plugins-reload-result")).toBeNull());
  });

  it("retires the recording report once a later act has its own answer", async () => {
    mockBridge({
      listings: [LISTING, LISTING, LISTING],
      sync: {
        status: 200,
        body: {
          recorded: ["genus-hostinfo"],
          added: [],
          updated: ["genus-hostinfo"],
          removed: [],
          reloaded: false,
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-record"));
    await screen.findByTestId("plugins-record-result");

    fireEvent.click(screen.getByTestId("plugins-reload"));
    await screen.findByTestId("plugins-reload-result");
    // Two reports describing two different moments, side by side, is how a page
    // ends up asserting a state that no longer exists.
    expect(screen.queryByTestId("plugins-record-result")).toBeNull();
  });

  it("says the engine kept its previous plugins when the reload itself failed", async () => {
    mockBridge({
      listings: [LISTING],
      reload: { status: 200, body: { generation: null, loaded: 0, failures: [] } },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const failed = await screen.findByTestId("plugins-reload-failed");
    expect(failed.textContent).toMatch(/kept its previous plugins/i);
  });
});

describe("Settings › Plugins — the states that are not a listing", () => {
  it("renders a 403 as muted text, not as a fault", async () => {
    mockBridge({ listStatus: 403 });
    render(<PluginsPage visible />);

    const forbidden = await screen.findByTestId("plugins-forbidden");
    expect(forbidden.className).toContain("muted");
    expect(forbidden.className).not.toContain("destructive");
    expect(screen.queryByTestId("plugins-error")).toBeNull();
  });

  it("says the engine is unreachable on a 502", async () => {
    mockBridge({ listStatus: 502 });
    render(<PluginsPage visible />);

    const error = await screen.findByTestId("plugins-error");
    expect(error.textContent).toMatch(/engine/i);
    expect(error.textContent).toMatch(/unreachable|did not answer|could not be reached/i);
  });

  it("reads nothing at all while the page is not on screen", () => {
    const recorded = mockBridge({});
    render(<PluginsPage visible={false} />);
    expect(recorded.listReads).toBe(0);
  });

  it("says so when nothing is installed", async () => {
    mockBridge({
      listings: [
        {
          generation: 1,
          lockfile: { path_configured: true, present: true, malformed: false, rows: 0 },
          plugins: [],
        },
      ],
    });
    render(<PluginsPage visible />);

    expect((await screen.findByTestId("plugins-empty")).textContent).toMatch(/no plugin/i);
  });
});

/**
 * Install from the registry.
 *
 * The rules, and what each is protecting against:
 *
 * * **Preview is a dry run, and it is the only way to reach Install.** The
 *   operator sees the artifact, its hash, whose key signed the index, and
 *   every reason the scanner found before anything is installed for real.
 * * **The verdict is a WORD, never a checkmark.** `safe` means "contributes
 *   tools and touches nothing outside this process" — a narrow claim about a
 *   static scan, which a tick would inflate into a clearance.
 * * **`review` is the common path.** Anything importing `os`, or whose
 *   manifest omits `entry_points:`, lands there, so it is gated by an explicit
 *   acceptance rather than treated as an error.
 * * **`blocked` has no button.** Not a disabled Install with a tooltip: the
 *   card says the plan will not be installed.
 * * **Every refusal is the server's own sentence.** A 504 in particular means
 *   "we stopped waiting; it may still be running" — rendering it as "failed"
 *   would tell an operator to retry an install that is in flight.
 */
describe("Settings › Plugins — installing from the registry", () => {
  async function preview(over: Partial<typeof REVIEW_PLAN.plan> = {}) {
    const recorded = mockBridge({
      listings: [LISTING, LISTING],
      install: [{ status: 200, body: { ...REVIEW_PLAN, plan: { ...REVIEW_PLAN.plan, ...over } } }],
    });
    render(<PluginsPage visible />);
    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");
    return recorded;
  }

  it("posts a dry run and renders the artifact, the hash, the key and every reason", async () => {
    const recorded = await preview();

    const posted = recorded.posts.find((p) => p.url.endsWith("/install"));
    expect(posted?.body).toMatchObject({ name: "genus-weather", dry_run: true });

    const plan = screen.getByTestId("plugins-install-plan");
    expect(plan.textContent).toContain("genus_weather-1.4.0-py3-none-any.whl");
    // The first twelve characters, which is what a person can compare.
    expect(plan.textContent).toContain("abc123def456");
    expect(plan.textContent).not.toContain(REVIEW_PLAN.plan.sha256);
    expect(plan.textContent).toContain("genus-2026");

    const reasons = screen.getByTestId("plugins-install-reasons");
    expect(reasons.textContent).toContain("genus_weather/__init__.py:4: imports os");
    expect(reasons.textContent).toContain("the manifest declares no entry_points:");
  });

  it("draws the verdict as a word and never as a tick", async () => {
    await preview();
    const pill = screen.getByTestId("plugins-install-verdict");
    expect(pill).toHaveAttribute("data-verdict", "review");
    expect(pill.textContent).toMatch(/review/i);
    expect(screen.getByTestId("plugins-install-card").textContent).not.toMatch(/[✓✔☑]/);
  });

  it("keeps Install dead until the operator accepts the review findings out loud", async () => {
    await preview();

    expect(screen.getByTestId("plugins-install-submit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    expect(screen.getByTestId("plugins-install-submit")).toBeEnabled();
  });

  it("asks for no acceptance when the plan is safe", async () => {
    await preview({ verdict: "safe", reasons: [] });

    expect(screen.queryByTestId("plugins-install-accept")).toBeNull();
    expect(screen.getByTestId("plugins-install-submit")).toBeEnabled();
  });

  it("offers no Install at all for a blocked plan, and says so", async () => {
    await preview({ verdict: "blocked", reasons: ["acme/__init__.py:2: calls os.system"] });

    expect(screen.queryByTestId("plugins-install-submit")).toBeNull();
    expect(screen.queryByTestId("plugins-install-accept")).toBeNull();
    const plan = screen.getByTestId("plugins-install-plan");
    expect(plan.textContent).toMatch(/will not be installed|cannot be installed|refuses/i);
    expect(plan.textContent).toContain("acme/__init__.py:2: calls os.system");
  });

  it("posts the acceptance, shows the recorded row, and raises the reload bar", async () => {
    const recorded = mockBridge({
      listings: [LISTING, LISTING],
      install: [
        { status: 200, body: REVIEW_PLAN },
        { status: 200, body: INSTALLED_REPLY },
      ],
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.change(screen.getByTestId("plugins-install-version"), { target: { value: "1.4.0" } });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");

    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    fireEvent.click(screen.getByTestId("plugins-install-submit"));

    const result = await screen.findByTestId("plugins-install-result");
    const install = recorded.posts.filter((p) => p.url.endsWith("/install")).at(-1);
    expect(install?.body).toMatchObject({
      name: "genus-weather",
      version: "1.4.0",
      accept_review: true,
      dry_run: false,
    });
    // The recorded lock row, which is the governance record that was written.
    expect(result.textContent).toContain("genus-weather");
    expect(result.textContent).toContain("1.4.0");
    // Installing does not load: the engine is still serving what it discovered.
    await screen.findByTestId("plugins-reload-bar");
    await waitFor(() => expect(recorded.listReads).toBeGreaterThan(1));
  });

  it("renders a refusal as the server's own sentence", async () => {
    const detail =
      "the wheel genus_weather-1.4.0-py3-none-any.whl does not hash to what the index pinned; " +
      "nothing was installed";
    mockBridge({ listings: [LISTING], install: [{ status: 422, body: { detail } }] });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));

    expect((await screen.findByTestId("plugins-install-error")).textContent).toContain(detail);
    expect(screen.queryByTestId("plugins-install-plan")).toBeNull();
  });

  it("does not turn a 504 into 'failed', because the install may still be running", async () => {
    const detail =
      "the plugin operation did not finish within 60s and was cancelled; it may still be " +
      "running, so check the listing before retrying";
    mockBridge({ listings: [LISTING], install: [{ status: 504, body: { detail } }] });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));

    const error = await screen.findByTestId("plugins-install-error");
    expect(error.textContent).toContain("it may still be running");
    expect(error.textContent).not.toMatch(/\bfailed\b/i);
  });

  it("offers no index control when the instance reads exactly one", async () => {
    mockBridge({ listings: [LISTING] });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    expect(screen.queryByTestId("plugins-install-index")).toBeNull();
  });

  it("offers a choice, never a URL box, when more than one index is configured", async () => {
    const SECOND = "https://acme.example.com/plugins/index.json";
    const recorded = mockBridge({
      listings: [{ ...LISTING, indexes: [INDEX, SECOND] }, LISTING],
      install: [{ status: 200, body: REVIEW_PLAN }],
    });
    render(<PluginsPage visible />);

    const picker = await screen.findByTestId("plugins-install-index");
    expect(picker.tagName).toBe("SELECT");
    fireEvent.change(picker, { target: { value: SECOND } });
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));

    await screen.findByTestId("plugins-install-plan");
    expect(recorded.posts.find((p) => p.url.endsWith("/install"))?.body).toMatchObject({
      index: SECOND,
    });
  });

  it("will not preview without a name", async () => {
    mockBridge({ listings: [LISTING] });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    expect(screen.getByTestId("plugins-install-preview")).toBeDisabled();
  });

  it("retires a plan the moment the request it described changes", async () => {
    await preview();
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-other" },
    });
    // A plan is an answer about one request. Leaving it up would let an
    // operator accept findings about a wheel they are no longer installing.
    await waitFor(() => expect(screen.queryByTestId("plugins-install-plan")).toBeNull());
  });

  it("says where a local wheel goes, because that act is CLI-only", async () => {
    mockBridge({ listings: [LISTING] });
    render(<PluginsPage visible />);

    const card = await screen.findByTestId("plugins-install-card");
    expect(card.textContent).toContain("genus plugin install ./x.whl --sha256");
  });

  /** Plan B: a DIFFERENT wheel, with findings nobody has read yet. */
  const PLAN_B = {
    ...REVIEW_PLAN.plan,
    version: "9.9.9",
    sha256: "ffffffffffff" + "1".repeat(52),
    filename: "genus_weather-9.9.9-py3-none-any.whl",
    reasons: ["genus_weather/net.py:2: opens a socket", "genus_weather/run.py:9: calls subprocess"],
  };

  /**
   * The accept-review gate is the one binding constraint of this card, and
   * pressing the obvious button twice used to walk straight past it.
   *
   * `changed()` cleared the acceptance on an EDIT, which is the rarer gesture:
   * `version` left blank means "latest", so re-Previewing is exactly how an
   * operator re-checks before committing. A second Preview left the checkbox
   * bearing an acceptance of a different wheel's findings.
   */
  it("clears the acceptance when a second Preview answers with a different plan", async () => {
    const recorded = mockBridge({
      listings: [LISTING, LISTING],
      install: [
        { status: 200, body: REVIEW_PLAN },
        { status: 200, body: { ...REVIEW_PLAN, plan: PLAN_B } },
      ],
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");
    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    expect(screen.getByTestId("plugins-install-submit")).toBeEnabled();

    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await waitFor(() =>
      expect(screen.getByTestId("plugins-install-plan").textContent).toContain("9.9.9")
    );

    expect(screen.getByTestId("plugins-install-accept")).not.toBeChecked();
    expect(screen.getByTestId("plugins-install-submit")).toBeDisabled();
    expect(recorded.posts.filter((p) => p.body.dry_run === false)).toHaveLength(0);

    // And it is reachable again, for the findings that are now on screen.
    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    expect(screen.getByTestId("plugins-install-submit")).toBeEnabled();
  });

  it("never sends accept_review for a plan other than the one on screen", async () => {
    const recorded = mockBridge({
      listings: [LISTING, LISTING],
      install: [
        { status: 200, body: REVIEW_PLAN },
        { status: 200, body: { ...REVIEW_PLAN, plan: PLAN_B } },
        { status: 200, body: INSTALLED_REPLY },
      ],
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");
    fireEvent.click(screen.getByTestId("plugins-install-accept"));

    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await waitFor(() =>
      expect(screen.getByTestId("plugins-install-plan").textContent).toContain("9.9.9")
    );
    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    fireEvent.click(screen.getByTestId("plugins-install-submit"));

    await screen.findByTestId("plugins-install-result");
    // The acceptance is bound to a HASH, not to a checkbox: the request can
    // only carry it for the plan whose findings were the ones displayed.
    const real = recorded.posts.filter((p) => p.body.dry_run === false);
    expect(real).toHaveLength(1);
    expect(real[0].body).toMatchObject({ name: "genus-weather", accept_review: true });
  });

  it("will not fire a second real install once one has been answered", async () => {
    const recorded = mockBridge({
      listings: [LISTING, LISTING],
      install: [
        { status: 200, body: REVIEW_PLAN },
        { status: 200, body: INSTALLED_REPLY },
      ],
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");
    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    fireEvent.click(screen.getByTestId("plugins-install-submit"));
    await screen.findByTestId("plugins-install-result");

    // `pip install` twice over is how a half-written distribution happens, and
    // the only signal the act already ran is one line below the fold on a
    // phone. The button is the wrong place to leave armed.
    expect(screen.getByTestId("plugins-install-submit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("plugins-install-submit"));
    expect(recorded.posts.filter((p) => p.body.dry_run === false)).toHaveLength(1);
  });

  it("posts nothing more than the preview needs", async () => {
    const recorded = await preview();
    // `accept_review` on a dry run is a field the preview has no business
    // sending: nothing can be accepted before the findings exist.
    expect(Object.keys(recorded.posts[0].body).sort()).toEqual(["dry_run", "name"]);
  });

  it("posts the index the picker is showing, not whichever one the engine tries first", async () => {
    const SECOND = "https://acme.example.com/plugins/index.json";
    const recorded = mockBridge({
      listings: [{ ...LISTING, indexes: [INDEX, SECOND] }, LISTING],
      install: [{ status: 200, body: REVIEW_PLAN }],
    });
    render(<PluginsPage visible />);

    // The operator touches nothing: the select shows the first index, and an
    // omitted `index` makes the engine search ALL of them in order — so the
    // wheel can come from a different one than the control names.
    await screen.findByTestId("plugins-install-index");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));

    await screen.findByTestId("plugins-install-plan");
    expect(recorded.posts[0].body).toMatchObject({ index: INDEX });
  });

  it("prints the index the plan actually came from", async () => {
    await preview();
    // The one field that answers "where did this wheel come from" on a card
    // whose premise is that provenance is checkable.
    expect(screen.getByTestId("plugins-install-plan").textContent).toContain(INDEX);
  });

  it("offers no index it could not post, and says why", async () => {
    const HTTP = "http://internal.invalid/index.json";
    mockBridge({ listings: [{ ...LISTING, indexes: [INDEX, HTTP] }] });
    render(<PluginsPage visible />);

    const card = await screen.findByTestId("plugins-install-card");
    // `install` refuses a non-https index with a 422, so offering one is
    // offering a choice that cannot work.
    expect(screen.queryByTestId("plugins-install-index")).toBeNull();
    expect(card.textContent).toContain(HTTP);
    expect(card.textContent).toMatch(/https/i);
  });

  it("does not claim a hash is copied once a different one is on screen", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    mockBridge({
      listings: [LISTING, LISTING],
      install: [
        { status: 200, body: REVIEW_PLAN },
        { status: 200, body: { ...REVIEW_PLAN, plan: PLAN_B } },
      ],
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");

    fireEvent.click(screen.getByTestId("plugins-install-copy-sha"));
    await waitFor(() =>
      expect(screen.getByTestId("plugins-install-copy-sha").textContent).toMatch(/copied/i)
    );

    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await waitFor(() =>
      expect(screen.getByTestId("plugins-install-plan").textContent).toContain("ffffffffffff")
    );
    // The clipboard still holds plan A's hash. On the one control whose purpose
    // is comparing a hash, a stale affirmative is worse than no affordance.
    expect(screen.getByTestId("plugins-install-copy-sha").textContent).not.toMatch(/copied/i);
  });

  it("renders the route's note on an install that was not clean", async () => {
    const note = "the distribution was installed, but its lockfile row could not be recorded";
    mockBridge({
      listings: [LISTING, LISTING],
      install: [
        { status: 200, body: REVIEW_PLAN },
        { status: 200, body: { ...INSTALLED_REPLY, note } },
      ],
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugins-install-card");
    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");
    fireEvent.click(screen.getByTestId("plugins-install-accept"));
    fireEvent.click(screen.getByTestId("plugins-install-submit"));

    expect((await screen.findByTestId("plugins-install-result")).textContent).toContain(note);
  });
});

describe("Settings › Plugins — removing what the platform installed", () => {
  const WITH_SOURCE = { ...LISTING, plugins: [LOADED, FROM_REGISTRY] };

  it("offers Remove only for a row this platform installed", async () => {
    mockBridge({ listings: [WITH_SOURCE] });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-weather");
    expect(screen.getByTestId("plugin-remove-genus-weather")).toBeInTheDocument();
    // `remove` refuses a row with no `source` without --force, so a button here
    // would promise an act whose only outcome is a 422.
    expect(screen.queryByTestId("plugin-remove-genus-hostinfo")).toBeNull();
  });

  it("says where a registry-installed plugin came from", async () => {
    mockBridge({ listings: [WITH_SOURCE] });
    render(<PluginsPage visible />);

    const source = await screen.findByTestId("plugin-source-genus-weather");
    expect(source.textContent).toMatch(/registry/i);
    expect(source.textContent).toContain("genus-2026");
  });

  it("confirms inline before it posts anything", async () => {
    const recorded = mockBridge({ listings: [WITH_SOURCE] });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-remove-genus-weather"));
    await screen.findByTestId("plugin-remove-confirm-genus-weather");
    expect(recorded.posts.filter((p) => p.url.endsWith("/remove"))).toHaveLength(0);

    fireEvent.click(screen.getByTestId("plugin-remove-no-genus-weather"));
    await waitFor(() =>
      expect(screen.queryByTestId("plugin-remove-confirm-genus-weather")).toBeNull()
    );
    expect(recorded.posts.filter((p) => p.url.endsWith("/remove"))).toHaveLength(0);
  });

  it("posts remove, re-reads the listing, and raises the reload bar", async () => {
    const recorded = mockBridge({
      listings: [WITH_SOURCE, { ...LISTING, plugins: [LOADED] }],
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-remove-genus-weather"));
    fireEvent.click(await screen.findByTestId("plugin-remove-yes-genus-weather"));

    await waitFor(() =>
      expect(recorded.posts.map((p) => p.url)).toContain(
        "/api/bridge/api/plugins/genus-weather/remove"
      )
    );
    // Uninstalling does not unload: the engine keeps serving what it imported.
    await screen.findByTestId("plugins-reload-bar");
    await waitFor(() => expect(recorded.listReads).toBeGreaterThan(1));
  });

  it("lands a refusal on the row in the server's own words", async () => {
    const detail =
      "genus-weather has no recorded source, so this platform did not install it; " +
      "use genus plugin remove --force on the box if you mean to drop the row anyway";
    mockBridge({ listings: [WITH_SOURCE], remove: { status: 422, body: { detail } } });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-remove-genus-weather"));
    fireEvent.click(await screen.findByTestId("plugin-remove-yes-genus-weather"));

    expect((await screen.findByTestId("plugin-error-genus-weather")).textContent).toContain(detail);
  });

  it("renders the route's note when the removal was not clean", async () => {
    const note =
      "pip reported the distribution was not installed; the lockfile row was dropped anyway";
    mockBridge({
      listings: [WITH_SOURCE, { ...LISTING, plugins: [LOADED] }],
      remove: {
        status: 200,
        body: {
          name: "genus-weather",
          removed: false,
          row_dropped: true,
          reload_hint: "reload the engine (SIGHUP) or restart to apply",
          note,
        },
      },
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-remove-genus-weather"));
    fireEvent.click(await screen.findByTestId("plugin-remove-yes-genus-weather"));

    // The card is gone once the listing is re-read, so a row-scoped note would
    // be invisible: a partial removal has to be reported by the page.
    const result = await screen.findByTestId("plugins-remove-result");
    expect(result.textContent).toContain(note);
    expect(result.textContent).toMatch(/genus-weather/);
  });

  it("closes an open confirmation as soon as another act starts", async () => {
    const recorded = mockBridge({
      listings: [WITH_SOURCE, WITH_SOURCE],
      install: [{ status: 200, body: REVIEW_PLAN }],
    });
    render(<PluginsPage visible />);

    fireEvent.click(await screen.findByTestId("plugin-remove-genus-weather"));
    await screen.findByTestId("plugin-remove-confirm-genus-weather");

    fireEvent.change(screen.getByTestId("plugins-install-name"), {
      target: { value: "genus-weather" },
    });
    fireEvent.click(screen.getByTestId("plugins-install-preview"));
    await screen.findByTestId("plugins-install-plan");

    // A destructive confirmation sitting under a fresh install plan is one
    // mis-click from an act the operator has moved on from.
    expect(screen.queryByTestId("plugin-remove-confirm-genus-weather")).toBeNull();
    expect(recorded.posts.filter((p) => p.url.endsWith("/remove"))).toHaveLength(0);
  });
});

/**
 * `lockfile.problem` for damaged ROWS.
 *
 * `read_lockfile` leaves `malformed` FALSE when the file parses but individual
 * rows do not: the readable rows still govern, and the unreadable ones are
 * decisions that cannot be honoured — so whatever they turned off is loading
 * right now. The engine says so in `problem`; a page that rendered `problem`
 * only under `malformed` dropped exactly that sentence and reported a healthy
 * file with a row count beside it.
 */
describe("Settings › Plugins — a lockfile with unreadable rows", () => {
  const DAMAGED_ROWS = {
    ...LISTING,
    lockfile: {
      path_configured: true,
      present: true,
      malformed: false,
      rows: 1,
      problem: "holds 1 row(s) that cannot be read (position(s) 1)",
    },
  };

  it("prints the engine's sentence although the file itself parsed", async () => {
    mockBridge({ listings: [DAMAGED_ROWS] });
    render(<PluginsPage visible />);

    const warning = await screen.findByTestId("plugins-lockfile-rows-damaged");
    expect(warning.textContent).toContain("holds 1 row(s) that cannot be read (position(s) 1)");
    // Not the whole-file card: in this state the readable rows DO still govern,
    // and saying otherwise would be the opposite lie.
    expect(screen.queryByTestId("plugins-lockfile-malformed")).toBeNull();
    expect(warning.className).not.toContain("destructive");
  });

  it("does not report a partly unreadable lockfile as a healthy row count", async () => {
    mockBridge({ listings: [DAMAGED_ROWS] });
    render(<PluginsPage visible />);

    const chip = await screen.findByTestId("plugins-lockfile");
    expect(chip.textContent).toMatch(/could not be read|unreadable|damaged/i);
  });

  it("keeps the whole-file card for whole-file damage", async () => {
    mockBridge({
      listings: [
        {
          ...LISTING,
          lockfile: {
            path_configured: true,
            present: true,
            malformed: true,
            rows: 0,
            problem: "is not valid JSON (JSONDecodeError)",
          },
        },
      ],
    });
    render(<PluginsPage visible />);

    const warning = await screen.findByTestId("plugins-lockfile-malformed");
    expect(warning.textContent).toContain("is not valid JSON (JSONDecodeError)");
    expect(screen.queryByTestId("plugins-lockfile-rows-damaged")).toBeNull();
  });
});
