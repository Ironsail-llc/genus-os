"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Brain, Loader2, RefreshCw, Search } from "lucide-react";

import { EmptyState } from "@/components/business/empty-state";
import { PageHeader } from "@/components/business/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { isOperatorRole } from "@/components/layout/nav-config";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { useRowActions } from "@/lib/bridge/row-actions";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";

/**
 * Observe › Memory — what this tenant believes, and the one way to stop
 * believing it.
 *
 * Three routes, and a shape to each of them that this page has to respect
 * rather than paper over:
 *
 * * `GET /api/memory/facts` has **two orders**, not one. Without `q` it is
 *   `id DESC` with keyset pagination; with `q` it is a relevance ranking, and
 *   `next_cursor` is then always `null` — sending a `cursor` alongside a `q` is
 *   a 422, because a ranking has no keyset. So the pager is not merely empty in
 *   search mode, it is **absent**, and the lever a ranked page has instead is
 *   `limit`.
 * * `q` and `entity` **compose**, which means a searched page can come back
 *   shorter than `limit` for a reason that has nothing to do with how many
 *   facts match. Nothing on this page counts results or says "N of M".
 * * `POST …/forget` **bounds** a fact — `is_active=false`, `valid_to=now()` —
 *   on that row and no other. Nothing is deleted and supersession chains are
 *   not followed, so the confirmation says what it does and, more importantly,
 *   what it does NOT do: an always-in-context memory block that QUOTES the
 *   text still quotes it afterwards, and an agent carrying that block will keep
 *   repeating the fact. That warning is the reason the preview exists.
 *
 * `references.blocks_scanned: false` is not "no blocks". Below twelve
 * characters the bridge skips the scan, because `strpos(content, '')` is 1 in
 * Postgres and a stub fact reported every block on the instance as citing it.
 * A warning that cries wolf is the one that gets ignored, so a short fact says
 * "too short to check" and warns about nothing.
 *
 * No optimistic updates anywhere: a row changes because the forget's own
 * response said so, or because a 409 said the row was already stale.
 */

const BRIDGE = "/api/bridge";

/** Every `q` request costs an embedding call, so the box waits for a pause. */
export const SEARCH_DEBOUNCE_MS = 400;

const PAGE_LIMIT = 50;
/** The route's own ceiling. The only lever a ranked page has. */
const WIDE_LIMIT = 200;

const REASON_MIN = 3;
const REASON_MAX = 500;

type ActiveFilter = "true" | "false" | "all";

const ACTIVE_FILTERS: Array<{ id: ActiveFilter; label: string }> = [
  { id: "true", label: "Active" },
  { id: "false", label: "Inactive" },
  { id: "all", label: "All" },
];

export interface Fact {
  id: number;
  factText: string;
  entities: string[];
  isActive: boolean;
  validFrom: string | null;
  validTo: string | null;
  confidence: number | null;
  source: string | null;
  createdAt: string | null;
  supersededBy: number | null;
}

interface References {
  entities: string[];
  episodes: number;
  blocks: string[];
  blocksScanned: boolean;
}

interface Preview {
  fact: Fact | null;
  wouldDeactivate: number[];
  references: References;
  alreadyInactive: boolean;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** One fact, however thin the row that produced it turns out to be. */
export function normalizeFact(entry: unknown): Fact | null {
  if (!entry || typeof entry !== "object") return null;
  const row = entry as Record<string, unknown>;
  if (typeof row.id !== "number") return null;
  return {
    id: row.id,
    factText: typeof row.fact_text === "string" ? row.fact_text : "",
    entities: strings(row.entities),
    // Absent is not active: a row whose state cannot be read must not be
    // rendered as one an operator still has to act on.
    isActive: row.is_active === true,
    validFrom: str(row.valid_from),
    validTo: str(row.valid_to),
    confidence: typeof row.confidence === "number" ? row.confidence : null,
    source: str(row.source),
    createdAt: str(row.created_at),
    supersededBy: typeof row.superseded_by === "number" ? row.superseded_by : null,
  };
}

function normalizePreview(body: unknown): Preview {
  const root = (body ?? {}) as Record<string, unknown>;
  const refs = (root.references ?? {}) as Record<string, unknown>;
  return {
    fact: normalizeFact(root.fact),
    wouldDeactivate: Array.isArray(root.would_deactivate)
      ? root.would_deactivate.filter((v): v is number => typeof v === "number")
      : [],
    references: {
      entities: strings(refs.entities),
      episodes: typeof refs.episodes === "number" ? refs.episodes : 0,
      blocks: strings(refs.blocks),
      // Absent reads as NOT scanned, so an older bridge warns about nothing
      // rather than warning about everything.
      blocksScanned: refs.blocks_scanned === true,
    },
    alreadyInactive: root.already_inactive === true,
  };
}

function when(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export interface FactsQuery {
  query: string;
  entity: string;
  active: ActiveFilter;
  limit: number;
  cursor?: string;
}

/**
 * The one place a facts request is spelled.
 *
 * Its own exported function rather than a closure inside the component so the
 * rule below can be tested for what it is: `cursor` is **dropped** when `q` is
 * set, because the route answers that pair with a 422 — "cursor cannot be
 * combined with q; a relevance ranking has no keyset". The page also hides the
 * pager in search mode, so the two of them are belt and braces; a guard that
 * can only be reached through a control that is not on the screen is exactly
 * the kind this repo has shipped inert before, and this one is reachable from
 * a test.
 */
export function factsUrl({ query, entity, active, limit, cursor }: FactsQuery): string {
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  if (entity) params.set("entity", entity);
  params.set("active", active);
  params.set("limit", String(limit));
  if (cursor && !query) params.set("cursor", cursor);
  return `${BRIDGE}/api/memory/facts?${params.toString()}`;
}

export interface MemoryViewProps {
  /** The shell keeps every view mounted; this is the load gate. */
  visible?: boolean;
  /** Session role. UX only — the bridge gates every memory route itself. */
  role?: string | null;
  /** The session has not resolved yet, so the role is unknown — not "denied". */
  roleLoading?: boolean;
}

export function MemoryView({ visible = true, role, roleLoading = false }: MemoryViewProps) {
  const [facts, setFacts] = useState<Fact[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paging, setPaging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * The bridge says this listing is not this caller's. Not a failure — the
   * rule `use-bridge-poll`'s header states for the whole Helm, which this view
   * has to keep even though it hand-rolls its fetch. Reachable without any
   * bridge change: the Helm decides "operator" from the session role, and
   * `require_operator` also demands the platform tenant and a human session.
   */
  const [forbidden, setForbidden] = useState(false);

  // Two pairs on purpose: the DRAFT is what the operator is typing, the
  // applied value is what has been asked for. Only the applied pair is in the
  // request, and only it is a dependency of the load.
  const [queryDraft, setQueryDraft] = useState("");
  const [entityDraft, setEntityDraft] = useState("");
  const [query, setQuery] = useState("");
  const [entity, setEntity] = useState("");
  const [active, setActive] = useState<ActiveFilter>("true");
  const [limit, setLimit] = useState(PAGE_LIMIT);

  const [previews, setPreviews] = useState<Record<number, Preview>>({});
  const [reasons, setReasons] = useState<Record<number, string>>({});
  const reasonRefs = useRef<Record<number, HTMLInputElement | null>>({});

  const { busyRow, rowErrors, act, clearRow } = useRowActions();

  const operator = isOperatorRole(role);
  const mayRead = operator || roleLoading;

  useEffect(() => {
    if (queryDraft === query && entityDraft === entity) return;
    const timer = setTimeout(() => {
      setQuery(queryDraft);
      setEntity(entityDraft);
      setLimit(PAGE_LIMIT);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [queryDraft, entityDraft, query, entity]);

  const listUrl = useCallback(
    (cursor?: string) => factsUrl({ query, entity, active, limit, cursor }),
    [query, entity, active, limit]
  );

  const load = useCallback(
    async (cursor?: string) => {
      if (cursor) setPaging(true);
      else setLoading(true);
      try {
        const res = await fetch(listUrl(cursor));
        if (!res.ok) {
          if (res.status === 403) {
            setForbidden(true);
            setError(null);
            return;
          }
          setError(await readBridgeReply(res));
          return;
        }
        setForbidden(false);
        const body = (await res.json()) as Record<string, unknown>;
        const page = Array.isArray(body.facts)
          ? body.facts.map(normalizeFact).filter((f): f is Fact => f !== null)
          : [];
        setFacts((previous) => (cursor ? [...previous, ...page] : page));
        setNextCursor(typeof body.next_cursor === "string" ? body.next_cursor : null);
        setError(null);
      } catch {
        setError(BRIDGE_UNREACHABLE);
      } finally {
        setLoading(false);
        setPaging(false);
      }
    },
    [listUrl]
  );

  useEffect(() => {
    if (!visible || !operator) return;
    void load();
  }, [visible, operator, load]);

  /** The row as the SERVER last described it — never as this page hoped. */
  const replaceFact = useCallback((fact: Fact) => {
    setFacts((previous) => previous.map((row) => (row.id === fact.id ? fact : row)));
  }, []);

  const openPreview = useCallback(
    (fact: Fact) => {
      void act(
        String(fact.id),
        `${BRIDGE}/api/memory/facts/${fact.id}/forget/preview`,
        { method: "POST" },
        (body) => {
          setPreviews((previous) => ({ ...previous, [fact.id]: normalizePreview(body) }));
          setReasons((previous) => ({ ...previous, [fact.id]: previous[fact.id] ?? "" }));
        }
      );
    },
    [act]
  );

  const closePreview = useCallback(
    (id: number) => {
      setPreviews((previous) => {
        const next = { ...previous };
        delete next[id];
        return next;
      });
      clearRow(String(id));
    },
    [clearRow]
  );

  const confirmForget = useCallback(
    (id: number) => {
      const reason = reasons[id] ?? "";
      void act(
        String(id),
        `${BRIDGE}/api/memory/facts/${id}/forget`,
        { method: "POST", body: JSON.stringify({ reason }) },
        (body) => {
          const fact = normalizeFact((body as Record<string, unknown>)?.fact);
          if (fact) replaceFact(fact);
          setPreviews((previous) => {
            const next = { ...previous };
            delete next[id];
            return next;
          });
          setReasons((previous) => ({ ...previous, [id]: "" }));
        },
        (res) => {
          // 409 is not a failure to understand — it is the server saying this
          // row is already bounded, which makes the list, not the server, the
          // thing that is wrong. The sentence is already on the row; the list
          // catches up, and the card closes with it. Leaving the card open
          // under a fresh `inactive` pill would put "this fact is bounded" and
          // "Forget this fact" on the screen together, and the only thing a
          // second click could produce is a second 409.
          if (res.status === 409) {
            setFacts((previous) =>
              previous.map((row) => (row.id === id ? { ...row, isActive: false } : row))
            );
            setPreviews((previous) => {
              const next = { ...previous };
              delete next[id];
              return next;
            });
          }
          // Anything the server refused about the request itself belongs beside
          // the only field the operator can change.
          if (res.status === 422) reasonRefs.current[id]?.focus();
        }
      );
    },
    [act, reasons, replaceFact]
  );

  const searching = query.length > 0;
  const showPager = !searching && nextCursor !== null;
  const canWiden = searching && limit < WIDE_LIMIT;

  const body = useMemo(() => {
    if (forbidden) {
      return (
        <p className="text-xs text-muted-foreground" data-testid="memory-forbidden">
          The instance&rsquo;s memory is operator-only on this appliance, so there is nothing to show
          here. A role that looks like an operator in this browser can still be refused by the
          bridge — it also requires the platform tenant and a human session. Ask an owner or admin
          on this instance.
        </p>
      );
    }
    if (loading && facts.length === 0) {
      return (
        <div
          className="flex items-center gap-2 p-6 text-xs text-muted-foreground"
          data-testid="memory-loading"
        >
          <Loader2 aria-hidden className="size-4 animate-spin" />
          Reading what this instance believes…
        </div>
      );
    }
    if (facts.length === 0 && !error) {
      return (
        <EmptyState
          testId="memory-empty"
          icon={Brain}
          title={searching ? "Nothing matched that search" : "No facts recorded yet"}
          description={
            searching
              ? "The search is a relevance ranking over this tenant's facts, not a substring match — try fewer words, or widen the search."
              : "Facts are written by the memory pipeline as the fleet works. An instance that has not run yet has none."
          }
        />
      );
    }
    return null;
  }, [forbidden, loading, facts.length, error, searching]);

  if (!visible) return null;

  if (!mayRead) {
    return (
      <div className="flex h-full flex-col gap-3 overflow-y-auto p-4" data-testid="memory-view">
        <PageHeader title="Memory" />
        <EmptyState
          testId="memory-not-yours"
          icon={Brain}
          title="Memory is an operator screen"
          description="What the instance remembers, and the ability to make it stop remembering, belong to an owner or an admin. The bridge refuses these routes to everybody else, including an auditor."
        />
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4" data-testid="memory-view">
      <PageHeader title="Memory" description="What this instance believes, and where it came from.">
        <Button
          variant="outline"
          size="sm"
          onClick={() => void load()}
          data-testid="memory-refresh"
        >
          <RefreshCw aria-hidden className={loading ? "animate-spin" : undefined} />
          Refresh
        </Button>
      </PageHeader>

      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <span className="relative flex min-w-0 flex-1 items-center sm:max-w-sm">
          <Search
            aria-hidden
            className="pointer-events-none absolute left-2 size-3.5 text-muted-foreground"
          />
          <Input
            data-testid="memory-query"
            aria-label="Search facts"
            placeholder="Search facts"
            value={queryDraft}
            onChange={(event) => setQueryDraft(event.target.value)}
            className="h-8 w-full pl-7 text-sm"
          />
        </span>
        <Input
          data-testid="memory-entity"
          aria-label="Filter by entity"
          placeholder="Entity"
          value={entityDraft}
          onChange={(event) => setEntityDraft(event.target.value)}
          className="h-8 min-w-[120px] max-w-[200px] text-sm"
        />
        <div
          role="group"
          aria-label="Fact state"
          className="flex shrink-0 items-center gap-1 rounded-md border border-border p-0.5"
        >
          {ACTIVE_FILTERS.map((filter) => (
            <button
              key={filter.id}
              type="button"
              aria-pressed={active === filter.id}
              data-testid={`memory-active-${filter.id}`}
              onClick={() => {
                setActive(filter.id);
                setLimit(PAGE_LIMIT);
              }}
              className={`rounded px-2 py-1 text-xs transition-colors ${
                active === filter.id
                  ? "bg-primary/15 text-foreground"
                  : "text-muted-foreground hover:bg-accent/60"
              }`}
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>

      {searching ? (
        <p data-testid="memory-search-note" className="max-w-3xl text-[11px] text-muted-foreground">
          A search is one ranked page, not a paged list — relevance has no place to resume from, so
          there is no next page. Widen it to rank more candidates instead.
          {canWiden ? (
            <>
              {" "}
              <button
                type="button"
                data-testid="memory-widen"
                onClick={() => setLimit(WIDE_LIMIT)}
                className="underline underline-offset-2 hover:text-foreground"
              >
                Rank {WIDE_LIMIT} candidates
              </button>
            </>
          ) : null}
        </p>
      ) : null}

      {error ? (
        <p
          data-testid="memory-error"
          className="max-w-3xl rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive"
        >
          {error}
        </p>
      ) : null}

      {body}

      <div className="flex min-w-0 flex-col gap-2">
        {facts.map((fact) => {
          const id = String(fact.id);
          const preview = previews[fact.id];
          const reason = reasons[fact.id] ?? "";
          const trimmed = reason.trim().length;
          const reasonOk = trimmed >= REASON_MIN && reason.length <= REASON_MAX;

          return (
            <div
              key={fact.id}
              data-testid={`memory-fact-${fact.id}`}
              className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-border bg-card p-3"
            >
              <div className="flex min-w-0 flex-wrap items-start gap-2">
                <p className="min-w-0 flex-1 break-words text-sm text-foreground">
                  {fact.factText}
                </p>
                {!fact.isActive ? (
                  <span
                    data-testid={`memory-inactive-${fact.id}`}
                    className="shrink-0 rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[10px] font-medium text-warning"
                  >
                    inactive
                  </span>
                ) : null}
              </div>

              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                {fact.entities.map((name) => (
                  <span
                    key={name}
                    data-testid={`memory-chip-${fact.id}-${name}`}
                    className="rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground"
                  >
                    {name}
                  </span>
                ))}
                {fact.source ? (
                  <span className="text-[10px] text-muted-foreground">via {fact.source}</span>
                ) : null}
                {fact.confidence !== null ? (
                  <span className="text-[10px] text-muted-foreground">
                    {Math.round(fact.confidence * 100)}% confidence
                  </span>
                ) : null}
              </div>

              <p className="text-[10px] text-muted-foreground/80">
                Recorded {when(fact.createdAt)}
                {fact.validFrom ? ` · valid from ${when(fact.validFrom)}` : ""}
                {fact.validTo ? ` · bounded ${when(fact.validTo)}` : ""}
              </p>

              {fact.supersededBy !== null ? (
                <button
                  type="button"
                  data-testid={`memory-superseded-${fact.id}`}
                  onClick={() => {
                    // Not a link: the newer fact is a row in this same list, so
                    // the honest move is to go and find it rather than to
                    // navigate somewhere that does not exist.
                    setQueryDraft("");
                    setEntityDraft("");
                    setActive("all");
                  }}
                  className="self-start text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground"
                >
                  Superseded by fact #{fact.supersededBy}
                </button>
              ) : null}

              {rowErrors[id] ? (
                <p data-testid={`memory-error-${fact.id}`} className="text-[11px] text-destructive">
                  {rowErrors[id]}
                </p>
              ) : null}

              {fact.isActive && !preview ? (
                <div className="flex items-center gap-2 pt-0.5">
                  <Button
                    variant="outline"
                    size="sm"
                    data-testid={`memory-forget-${fact.id}`}
                    disabled={busyRow === id}
                    onClick={() => openPreview(fact)}
                  >
                    {busyRow === id ? "Checking…" : "Forget"}
                  </Button>
                </div>
              ) : null}

              {preview ? (
                <div
                  data-testid={`memory-confirm-${fact.id}`}
                  className="flex min-w-0 flex-col gap-2 rounded-md border border-border bg-muted/40 p-3"
                >
                  <p className="text-[11px] font-medium text-foreground">
                    Forget “{preview.fact?.factText ?? fact.factText}”?
                  </p>
                  <p className="text-[11px] text-muted-foreground">
                    This bounds the fact — it is marked inactive and given an end date. Nothing is
                    deleted, and no other fact changes:{" "}
                    {preview.wouldDeactivate.length === 1
                      ? "one row"
                      : `${preview.wouldDeactivate.length} rows`}{" "}
                    would be deactivated.
                  </p>
                  <p className="text-[11px] text-muted-foreground">
                    Filed under {preview.references.entities.join(", ") || "no entity"} ·{" "}
                    {preview.references.episodes} episode
                    {preview.references.episodes === 1 ? "" : "s"} cite it
                  </p>

                  {preview.references.blocksScanned && preview.references.blocks.length > 0 ? (
                    <p
                      data-testid={`memory-blocks-warning-${fact.id}`}
                      className="rounded-md border border-warning/30 bg-warning/10 p-2 text-[11px] text-warning"
                    >
                      Still quoted in {preview.references.blocks.length} always-in-context memory
                      block{preview.references.blocks.length === 1 ? "" : "s"} (
                      {preview.references.blocks.join(", ")}). Forgetting the fact does not edit a
                      block, so an agent carrying one will keep repeating this until the block is
                      rewritten.
                    </p>
                  ) : null}

                  {!preview.references.blocksScanned ? (
                    <p
                      data-testid={`memory-blocks-unscanned-${fact.id}`}
                      className="text-[11px] text-muted-foreground"
                    >
                      Too short to check against the memory blocks — a fragment this small matches
                      almost any text, so the bridge does not guess.
                    </p>
                  ) : null}

                  {preview.alreadyInactive ? (
                    <p
                      data-testid={`memory-already-inactive-${fact.id}`}
                      className="text-[11px] text-muted-foreground"
                    >
                      This fact is already inactive; there is nothing left to forget.
                    </p>
                  ) : null}

                  <div className="flex min-w-0 flex-col gap-1">
                    <Input
                      data-testid={`memory-reason-${fact.id}`}
                      aria-label="Reason for forgetting"
                      placeholder="Why is this being forgotten? (recorded in the audit log)"
                      value={reason}
                      ref={(element) => {
                        reasonRefs.current[fact.id] = element;
                      }}
                      onChange={(event) =>
                        setReasons((previous) => ({ ...previous, [fact.id]: event.target.value }))
                      }
                      className="h-8 w-full text-sm"
                    />
                    <span
                      data-testid={`memory-reason-count-${fact.id}`}
                      className={`text-[10px] ${
                        reason.length > REASON_MAX ? "text-destructive" : "text-muted-foreground"
                      }`}
                    >
                      {reason.length} / {REASON_MAX} — at least {REASON_MIN} characters
                    </span>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      data-testid={`memory-confirm-go-${fact.id}`}
                      disabled={!reasonOk || busyRow === id}
                      onClick={() => confirmForget(fact.id)}
                    >
                      {busyRow === id ? "Forgetting…" : "Forget this fact"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      data-testid={`memory-confirm-cancel-${fact.id}`}
                      onClick={() => closePreview(fact.id)}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>

      {showPager ? (
        <div className="flex justify-center pb-2">
          <Button
            variant="outline"
            size="sm"
            data-testid="memory-load-more"
            disabled={paging}
            onClick={() => void load(nextCursor ?? undefined)}
          >
            {paging ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}

      <p className="max-w-3xl pb-2 text-[11px] text-muted-foreground">
        Forgetting bounds a fact; it never deletes one. The row keeps its text and its provenance,
        gains an end date, and stops being retrieved. The audit record carries the fact id and the
        reason — never the text.
      </p>
    </div>
  );
}

export default MemoryView;
