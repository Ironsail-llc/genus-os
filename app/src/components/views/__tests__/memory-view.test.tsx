/**
 * Observe › Memory — what this tenant believes, and the one way to stop
 * believing it.
 *
 * The claims worth pinning, in the order they bite:
 *
 * * **the pager is hidden in search mode.** `GET /api/memory/facts` answers
 *   `next_cursor: null` for every `q`, and a `cursor` sent WITH a `q` is a 422
 *   ("a relevance ranking has no keyset"). A pager that stayed on screen would
 *   either do nothing or refetch page one forever.
 * * **a short page in search mode is not the end of the results.** `q` and
 *   `entity` compose — the ranking picks `limit` candidates and the entity
 *   predicate then removes some — so the page can be shorter than `limit`
 *   without meaning anything. Nothing here counts.
 * * **forget shows its consequences before it happens.** The preview is
 *   read-only and says which entities the fact is filed under, how many
 *   episodes cite it, and which always-in-context blocks QUOTE its text. That
 *   last one is the warning that matters: bounding the fact does not edit a
 *   block, so an agent carrying the text in context will keep repeating it.
 * * **`blocks_scanned: false` is not "no blocks".** Under twelve characters
 *   the scan is skipped, because `strpos(content, '')` is 1 in Postgres and a
 *   stub fact reported every block on the instance. It must say "too short to
 *   check", never warn.
 * * **nothing is optimistic.** The row changes from the forget's own response,
 *   or — on a 409 — because the server said it is already inactive.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MemoryView } from "../memory-view";

interface Fact {
  id: number;
  fact_text: string;
  entities: string[];
  is_active: boolean;
  valid_from: string | null;
  valid_to: string | null;
  confidence: number | null;
  source: string | null;
  created_at: string;
  superseded_by: number | null;
}

function fact(overrides: Partial<Fact> & { id: number }): Fact {
  return {
    fact_text: "Alice prefers tea",
    entities: ["Alice"],
    is_active: true,
    valid_from: null,
    valid_to: null,
    confidence: 0.92,
    source: "chat",
    created_at: "2026-09-15T12:00:00+00:00",
    superseded_by: null,
    ...overrides,
  };
}

const PAGE_ONE = {
  facts: [
    fact({ id: 4711 }),
    fact({ id: 4710, fact_text: "Bob works at Example Corp", entities: ["Bob", "Example Corp"] }),
    fact({
      id: 4709,
      fact_text: "Alice moved teams",
      is_active: false,
      valid_to: "2026-09-01T00:00:00+00:00",
      superseded_by: 4711,
    }),
  ],
  next_cursor: "4709",
};

const PAGE_TWO = { facts: [fact({ id: 4708, fact_text: "Bob prefers coffee" })], next_cursor: null };

const PREVIEW_WITH_BLOCKS = {
  fact: PAGE_ONE.facts[0],
  would_deactivate: [4711],
  references: {
    entities: ["Alice"],
    episodes: 3,
    blocks: ["working_context"],
    blocks_scanned: true,
  },
  already_inactive: false,
};

const requests: string[] = [];
const fetchMock = vi.fn();

/** Answers by URL, so a spec says what each route returns and nothing else. */
function respond(routes: Array<[RegExp, () => { status?: number; body: unknown }]>) {
  fetchMock.mockImplementation(async (url: string) => {
    requests.push(url);
    for (const [pattern, make] of routes) {
      if (pattern.test(url)) {
        const { status = 200, body } = make();
        return {
          ok: status >= 200 && status < 300,
          status,
          json: async () => body,
        } as Response;
      }
    }
    throw new Error(`no stub for ${url}`);
  });
}

function listOnly(body: unknown = PAGE_ONE) {
  respond([[/\/api\/memory\/facts\?/, () => ({ body })]]);
}

beforeEach(() => {
  requests.length = 0;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The last request whose URL matched — what the view actually asked for. */
function lastRequest(pattern: RegExp): string {
  const matched = requests.filter((url) => pattern.test(url));
  return matched[matched.length - 1] ?? "";
}

describe("Observe › Memory", () => {
  it("renders a fact with its entities, source, confidence and dates", async () => {
    listOnly();
    render(<MemoryView role="owner" />);

    const row = await screen.findByTestId("memory-fact-4711");
    expect(row.textContent).toContain("Alice prefers tea");
    expect(within(row).getByTestId("memory-chip-4711-Alice")).toBeTruthy();
    expect(row.textContent).toContain("chat");
    expect(row.textContent).toContain("92%");
  });

  it("marks an inactive fact and links what superseded it", async () => {
    listOnly();
    render(<MemoryView role="owner" />);

    const row = await screen.findByTestId("memory-fact-4709");
    expect(within(row).getByTestId("memory-inactive-4709")).toBeTruthy();
    const link = within(row).getByTestId("memory-superseded-4709");
    expect(link.textContent).toContain("4711");
    // A fact that is already bounded has nothing left to forget.
    expect(within(row).queryByTestId("memory-forget-4709")).toBeNull();
  });

  it("defaults to the active facts and sends the filters it is showing", async () => {
    listOnly();
    render(<MemoryView role="owner" />);

    await screen.findByTestId("memory-fact-4711");
    const url = lastRequest(/memory\/facts/);
    expect(url).toContain("active=true");
    expect(url).not.toContain("q=");
    expect(url).not.toContain("cursor=");

    fireEvent.click(screen.getByTestId("memory-active-all"));
    await waitFor(() => expect(lastRequest(/memory\/facts/)).toContain("active=all"));
  });

  it("pages with the keyset cursor and appends, never replaces", async () => {
    let page = 0;
    respond([[/\/api\/memory\/facts\?/, () => ({ body: page++ === 0 ? PAGE_ONE : PAGE_TWO })]]);
    render(<MemoryView role="owner" />);

    await screen.findByTestId("memory-fact-4711");
    fireEvent.click(screen.getByTestId("memory-load-more"));

    await screen.findByTestId("memory-fact-4708");
    expect(lastRequest(/memory\/facts/)).toContain("cursor=4709");
    // The first page is still there.
    expect(screen.getByTestId("memory-fact-4711")).toBeTruthy();
    // `next_cursor: null` ends it.
    await waitFor(() => expect(screen.queryByTestId("memory-load-more")).toBeNull());
  });

  it("hides the pager in search mode and never sends a cursor with a query", async () => {
    respond([
      [
        /\/api\/memory\/facts\?/,
        () => ({ body: /[?&]q=/.test(requests[requests.length - 1]) ? PAGE_TWO : PAGE_ONE }),
      ],
    ]);
    render(<MemoryView role="owner" />);

    await screen.findByTestId("memory-load-more");

    fireEvent.change(screen.getByTestId("memory-query"), { target: { value: "coffee" } });

    await screen.findByTestId("memory-fact-4708");
    await waitFor(() => expect(screen.queryByTestId("memory-load-more")).toBeNull());
    expect(screen.getByTestId("memory-search-note")).toBeTruthy();
    for (const url of requests.filter((u) => /[?&]q=/.test(u))) {
      expect(url).not.toContain("cursor=");
    }
  });

  it("debounces the search box — one request, not one per keystroke", async () => {
    listOnly();
    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");

    const box = screen.getByTestId("memory-query");
    for (const value of ["c", "co", "cof", "coff", "coffe", "coffee"]) {
      fireEvent.change(box, { target: { value } });
    }

    await waitFor(() => expect(lastRequest(/memory\/facts/)).toContain("q=coffee"));
    // Every `q` request costs an embedding call; six keystrokes must not be six.
    expect(requests.filter((u) => /[?&]q=/.test(u))).toHaveLength(1);
  });

  it("offers a wider search instead of a pager, and composes q with entity", async () => {
    listOnly(PAGE_ONE);
    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");

    fireEvent.change(screen.getByTestId("memory-query"), { target: { value: "tea" } });
    fireEvent.change(screen.getByTestId("memory-entity"), { target: { value: "Alice" } });
    await waitFor(() => expect(lastRequest(/memory\/facts/)).toContain("q=tea"));
    expect(lastRequest(/memory\/facts/)).toContain("entity=Alice");
    // `limit` is the only lever a ranked page has, so the note says so and the
    // button raises it. It never says "3 of N" — a q+entity page is short for a
    // reason that has nothing to do with how many results exist.
    expect(screen.getByTestId("memory-search-note").textContent).not.toMatch(/\bof\b\s*\d/);

    fireEvent.click(screen.getByTestId("memory-widen"));
    await waitFor(() => expect(lastRequest(/memory\/facts/)).toContain("limit=200"));
  });

  it("previews a forget before it happens, and warns about the blocks that quote it", async () => {
    respond([
      [/forget\/preview/, () => ({ body: PREVIEW_WITH_BLOCKS })],
      [/\/api\/memory\/facts\?/, () => ({ body: PAGE_ONE })],
    ]);
    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");

    fireEvent.click(screen.getByTestId("memory-forget-4711"));

    const card = await screen.findByTestId("memory-confirm-4711");
    expect(card.textContent).toContain("Alice prefers tea");
    expect(card.textContent).toContain("3");
    const warning = within(card).getByTestId("memory-blocks-warning-4711");
    expect(warning.textContent).toContain("working_context");
    // Bounding the fact does not edit a block, and the warning has to say so.
    expect(warning.textContent?.toLowerCase()).toMatch(/repeat|keep|context/);
    // Read-only: showing the preview must not have written anything.
    expect(requests.filter((u) => /\/forget$/.test(u))).toHaveLength(0);
  });

  it("says a short fact was not scanned rather than warning about no blocks", async () => {
    respond([
      [
        /forget\/preview/,
        () => ({
          body: {
            fact: fact({ id: 4711, fact_text: "tea" }),
            would_deactivate: [4711],
            references: { entities: ["Alice"], episodes: 0, blocks: [], blocks_scanned: false },
            already_inactive: false,
          },
        }),
      ],
      [/\/api\/memory\/facts\?/, () => ({ body: PAGE_ONE })],
    ]);
    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");
    fireEvent.click(screen.getByTestId("memory-forget-4711"));

    const card = await screen.findByTestId("memory-confirm-4711");
    expect(within(card).queryByTestId("memory-blocks-warning-4711")).toBeNull();
    expect(within(card).getByTestId("memory-blocks-unscanned-4711").textContent).toMatch(
      /too short/i
    );
  });

  it("requires a reason of 3 to 500 characters and counts it", async () => {
    respond([
      [/forget\/preview/, () => ({ body: PREVIEW_WITH_BLOCKS })],
      [/\/api\/memory\/facts\?/, () => ({ body: PAGE_ONE })],
    ]);
    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");
    fireEvent.click(screen.getByTestId("memory-forget-4711"));
    await screen.findByTestId("memory-confirm-4711");

    const confirm = screen.getByTestId("memory-confirm-go-4711");
    expect(confirm).toBeDisabled();

    const reason = screen.getByTestId("memory-reason-4711");
    fireEvent.change(reason, { target: { value: "no" } });
    expect(confirm).toBeDisabled();
    expect(screen.getByTestId("memory-reason-count-4711").textContent).toContain("2");

    fireEvent.change(reason, { target: { value: "x".repeat(501) } });
    expect(confirm).toBeDisabled();

    fireEvent.change(reason, { target: { value: "she asked for it to be dropped" } });
    expect(confirm).not.toBeDisabled();
  });

  it("forgets with the reason and updates the row from the response, not from hope", async () => {
    const forgotten = fact({
      id: 4711,
      is_active: false,
      valid_to: "2026-09-15T13:00:00+00:00",
    });
    const posted: Array<Record<string, unknown>> = [];
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      requests.push(url);
      if (/forget\/preview/.test(url)) {
        return { ok: true, status: 200, json: async () => PREVIEW_WITH_BLOCKS } as Response;
      }
      if (/\/forget$/.test(url)) {
        posted.push(JSON.parse(String(init?.body ?? "{}")));
        return {
          ok: true,
          status: 200,
          json: async () => ({ fact: forgotten, forgotten: true }),
        } as Response;
      }
      return { ok: true, status: 200, json: async () => PAGE_ONE } as Response;
    });

    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");
    fireEvent.click(screen.getByTestId("memory-forget-4711"));
    await screen.findByTestId("memory-confirm-4711");
    fireEvent.change(screen.getByTestId("memory-reason-4711"), {
      target: { value: "she asked for it to be dropped" },
    });
    fireEvent.click(screen.getByTestId("memory-confirm-go-4711"));

    await waitFor(() => expect(screen.getByTestId("memory-inactive-4711")).toBeTruthy());
    expect(posted).toEqual([{ reason: "she asked for it to be dropped" }]);
    expect(screen.queryByTestId("memory-confirm-4711")).toBeNull();
    // The other rows are untouched — one forget bounds one row.
    expect(screen.queryByTestId("memory-inactive-4710")).toBeNull();
  });

  it("renders a 409 in the server's own words and marks the row inactive", async () => {
    const sentence = "that fact is already inactive; there is nothing left to forget.";
    fetchMock.mockImplementation(async (url: string) => {
      requests.push(url);
      if (/forget\/preview/.test(url)) {
        return { ok: true, status: 200, json: async () => PREVIEW_WITH_BLOCKS } as Response;
      }
      if (/\/forget$/.test(url)) {
        return { ok: false, status: 409, json: async () => ({ detail: sentence }) } as Response;
      }
      return { ok: true, status: 200, json: async () => PAGE_ONE } as Response;
    });

    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");
    fireEvent.click(screen.getByTestId("memory-forget-4711"));
    await screen.findByTestId("memory-confirm-4711");
    fireEvent.change(screen.getByTestId("memory-reason-4711"), {
      target: { value: "she asked for it to be dropped" },
    });
    fireEvent.click(screen.getByTestId("memory-confirm-go-4711"));

    await waitFor(() => expect(screen.getByTestId("memory-error-4711").textContent).toBe(sentence));
    // The server just said the row is inactive; the list must agree with it.
    expect(screen.getByTestId("memory-inactive-4711")).toBeTruthy();
  });

  /**
   * The client's 3–500 rule is a courtesy that saves a round trip; it is not
   * the authority. The bridge strips before it counts, rejects a non-string,
   * and may tighten again — so when the two ever disagree the server wins, and
   * the operator has to be shown WHERE it disagreed rather than a banner at
   * the top of a list. The reason field is the only thing they can change.
   */
  it("lands a 422 on the reason field", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      requests.push(url);
      if (/forget\/preview/.test(url)) {
        return { ok: true, status: 200, json: async () => PREVIEW_WITH_BLOCKS } as Response;
      }
      if (/\/forget$/.test(url)) {
        return {
          ok: false,
          status: 422,
          json: async () => ({
            detail: "a reason of at least 3 characters is required to forget a fact",
          }),
        } as Response;
      }
      return { ok: true, status: 200, json: async () => PAGE_ONE } as Response;
    });

    render(<MemoryView role="owner" />);
    await screen.findByTestId("memory-fact-4711");
    fireEvent.click(screen.getByTestId("memory-forget-4711"));
    await screen.findByTestId("memory-confirm-4711");
    const reason = screen.getByTestId("memory-reason-4711");
    fireEvent.change(reason, { target: { value: "n/a" } });
    fireEvent.click(screen.getByTestId("memory-confirm-go-4711"));

    await waitFor(() => expect(screen.getByTestId("memory-error-4711")).toBeTruthy());
    expect(document.activeElement).toBe(reason);
    expect(screen.getByTestId("memory-error-4711").textContent).toContain("at least 3");
    // A refused forget is not a forget: the row stays exactly as it was.
    expect(screen.queryByTestId("memory-inactive-4711")).toBeNull();
  });

  it("says nothing matched rather than showing an empty list", async () => {
    listOnly({ facts: [], next_cursor: null });
    render(<MemoryView role="owner" />);
    expect((await screen.findByTestId("memory-empty")).textContent).toBeTruthy();
  });

  it("reads a refusal in the bridge's words", async () => {
    respond([
      [
        /\/api\/memory\/facts\?/,
        () => ({ status: 422, body: { detail: "limit must be between 1 and 200" } }),
      ],
    ]);
    render(<MemoryView role="owner" />);
    expect((await screen.findByTestId("memory-error")).textContent).toContain(
      "limit must be between 1 and 200"
    );
  });

  it("tells a non-operator whose screen this is, and asks the bridge nothing", async () => {
    listOnly();
    render(<MemoryView role="member" />);

    expect(await screen.findByTestId("memory-not-yours")).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
    // An auditor may read the audit trail; the memory is not theirs either.
    expect(screen.queryByTestId("memory-query")).toBeNull();
  });

  it("does not decide a role it has not been told yet", async () => {
    listOnly();
    render(<MemoryView role={undefined} roleLoading />);
    expect(screen.queryByTestId("memory-not-yours")).toBeNull();
  });

  it("touches nothing while it is not the view on screen", () => {
    listOnly();
    render(<MemoryView role="owner" visible={false} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
