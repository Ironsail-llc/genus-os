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

const LISTING = {
  generation: 3,
  lockfile: { path_configured: true, present: true, malformed: false, rows: 3 },
  plugins: [LOADED, OFF, DRIFTED],
};

/** A box where `genus plugin sync` has never run. */
const FRESH = {
  generation: 1,
  lockfile: { path_configured: true, present: false, malformed: false, rows: 0 },
  plugins: [{ ...UNRECORDED }, { ...LOADED, recorded: false, verdict: "" }],
};

interface Reply {
  status: number;
  body: unknown;
}

interface Recorded {
  posts: Array<{ url: string; method: string }>;
  listReads: number;
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
  listStatus?: number;
}): Recorded {
  const recorded: Recorded = { posts: [], listReads: 0 };
  const listings = options.listings ?? [LISTING];
  vi.spyOn(global, "fetch").mockImplementation((async (
    input: RequestInfo | URL,
    init?: RequestInit
  ) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (method === "POST") {
      recorded.posts.push({ url, method });
      const reply = url.endsWith("/sync")
        ? (options.sync ?? { status: 200, body: {} })
        : url.endsWith("/reload")
          ? (options.reload ?? { status: 200, body: { generation: 4, loaded: 1, failures: [] } })
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
            { name: "nightly_notes", group: "genus.jobs", reason: "disabled by operator" },
            { name: "widget_list", group: "genus.tools", reason: "ImportError: no module named x" },
          ],
        },
      },
    });
    render(<PluginsPage visible />);

    await screen.findByTestId("plugin-genus-hostinfo");
    fireEvent.click(screen.getByTestId("plugins-reload"));

    const report = await screen.findByTestId("plugins-reload-result");
    expect(report.textContent).toContain("2");

    // Matched back to the distribution through the listing's groups, not by
    // string equality with the entry-point name.
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
   * A `disabled by operator` refusal can only belong to a distribution whose
   * lock row is off — that is the only thing that produces it. The listing says
   * which those are, and the manifest names the entry points; a `-`/`_`-squashed
   * suffix match on the DISTRIBUTION name is the weakest evidence in the
   * payload and must not outrank either.
   */
  it("files the operator's own decision under the plugin they actually turned off", async () => {
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
          failures: [{ name: "nightly_notes", group: "genus.jobs", reason: "disabled by operator" }],
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
   * `inventory()` skips distributions the metadata layer cannot name while
   * `load_plugins` still loads — and still fails — their entry points, so a
   * refusal with no row behind it is reachable in production. Being alone in a
   * group is not evidence of having failed.
   */
  it("refuses to accuse the only distribution in a group when its manifest names something else", async () => {
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

  /**
   * The shape the box actually answers with.
   *
   * `manifest.declared` holds CONTRIBUTION names — `host_state` for
   * `genus-hostinfo` — while `failures[].name` is the ENTRY-POINT name,
   * `hostinfo`. So a manifest almost never names the failing entry point, and
   * an attribution rule that let a manifest's silence disqualify a
   * distribution from the name step sent every real refusal on a
   * manifest-bearing plugin to the unattributed bucket: the operator gets
   * "could not tie to an installed distribution" for the one plugin installed.
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
            { name: "widget_list", group: "genus.tools", reason: "ImportError: no module named x" },
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
            { name: "hostinfo", group: "genus.tools", reason: "disabled by operator" },
            { name: "hostinfo", group: "genus.services", reason: "SyntaxError: bad code" },
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
