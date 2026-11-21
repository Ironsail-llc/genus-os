"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CalendarRange, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { RoomSlotContext } from "@/components/rooms/room-slots";
import { refusalText } from "@/components/rooms/controls-shared";
import { editionError, useEditionArrival, type EditionRequest } from "@/components/rooms/use-edition-watch";
import { requestPeriod } from "@/lib/rooms/api";
import { formatPeriod } from "@/lib/rooms/format";
import {
  customPeriod,
  dayBefore,
  findStoredEdition,
  isFuturePeriod,
  isUnfinishedPeriod,
  lastCompletePeriod,
  resolvePeriod,
  todayIn,
  type FixedGrain,
  type Grain,
  type PeriodRange,
} from "@/lib/rooms/periods";

const GRAINS: Array<{ id: Grain; label: string }> = [
  { id: "day", label: "Day" },
  { id: "week", label: "Week" },
  { id: "month", label: "Month" },
  { id: "custom", label: "Custom" },
];
const GRAIN_LABEL: Record<Grain, string> = { day: "Day", week: "Week", month: "Month", custom: "Custom range" };

function settingString(settings: Record<string, unknown> | undefined, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = settings?.[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}

function isFixed(grain: unknown): grain is FixedGrain {
  return grain === "day" || grain === "week" || grain === "month";
}

/**
 * A report workspace's period picker: day, week, month or a custom range.
 *
 * A period that already has a published edition is shown at once, with no
 * request. Otherwise one `POST /api/rooms/{id}/periods` asks for it (the
 * engine joins a request for a period already building) and a "building"
 * line stays until it arrives, by the room's event or, failing that, a poll.
 * A period that has not ended yet is refused here, as the engine refuses it
 * (only a period whose last day is before today in the workspace's zone),
 * and the button stays off while one is picked; the daily on-demand cap and
 * any other refusal are said in the server's words.
 */
export function PeriodSelector({ ctx }: { ctx: RoomSlotContext }) {
  const { room, edition, editions, showEdition, refresh } = ctx;
  const tz = settingString(room.settings, "tz") ?? "UTC";
  const defaultGrain = settingString(room.settings, "default_grain", "defaultGrain");

  const [open, setOpen] = useState(false);
  const [grain, setGrain] = useState<Grain>("week");
  const [anchor, setAnchor] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<{ request: EditionRequest; range: PeriodRange } | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const errorId = useId();

  // Opening starts from the edition on show, or the last whole default period.
  const openPanel = () => {
    const today = todayIn(tz);
    const g: Grain = edition?.grain ?? (isFixed(defaultGrain) ? defaultGrain : "week");
    const fallback = lastCompletePeriod(isFixed(g) ? g : "week", today);
    setGrain(g);
    setAnchor(edition?.periodStart ?? fallback.start);
    setStart(edition?.periodStart ?? fallback.start);
    setEnd(edition?.periodEnd ?? fallback.end);
    setError(null);
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // The requested edition arrives among the published ones (by the room's
  // event, or the poll) and is shown; a failed build ends the wait.
  const arrival = useEditionArrival(room.id, pending?.request ?? null, editions, showEdition, refresh);
  const buildFailure =
    pending && arrival.failed
      ? `The edition for ${formatPeriod(pending.range.start, pending.range.end)} could not be built${editionError(arrival.failed) ? `: ${editionError(arrival.failed)}` : "."}`
      : null;
  // A new date replaces the last refusal; an unfinished pick is said live.
  const setDate = (set: (value: string) => void) => (value: string) => {
    set(value);
    setError(null);
  };
  const today = todayIn(tz);
  const lastEnded = dayBefore(today);
  const picked = grain === "custom" ? customPeriod(start, end) : anchor ? resolvePeriod(grain, anchor) : null;
  const unfinished = open && picked ? unfinishedText(picked, today) : null;
  const panelError = error ?? unfinished;
  const shownError = error ?? buildFailure;

  const apply = async () => {
    setPending(null);
    const range = grain === "custom" ? customPeriod(start, end) : resolvePeriod(grain, anchor);
    if (!range) {
      setError("Pick a start and an end, with the end on or after the start.");
      return;
    }
    const refusal = unfinishedText(range, todayIn(tz));
    if (refusal) {
      setError(refusal);
      return;
    }
    const stored = findStoredEdition(editions, range);
    if (stored) {
      setError(null);
      setOpen(false);
      showEdition(stored);
      return;
    }
    if (!ctx.can("request_period") || room.archived) {
      setError("There's no edition for this period, and your access can't ask for one.");
      return;
    }
    setSending(true);
    setError(null);
    try {
      const edition = await requestPeriod(room.id, range);
      if (edition.status === "failed") {
        setError(`The edition for ${formatPeriod(range.start, range.end)} could not be built${editionError(edition) ? `: ${editionError(edition)}` : "."}`);
        return;
      }
      setPending({ request: { key: Date.now(), id: edition.id, afterSeq: 0 }, range });
      setOpen(false);
      if (edition.status === "published") refresh();
    } catch (err) {
      setError(refusalText(err, "ask for that period"));
    } finally {
      setSending(false);
    }
  };

  const shownPeriod = edition ? formatPeriod(edition.periodStart, edition.periodEnd) : "";
  const label = edition ? `${GRAIN_LABEL[edition.grain ?? "custom"] ?? "Period"} · ${shownPeriod}` : "Pick a period";

  return (
    <div className="relative flex flex-wrap items-center justify-end gap-2" ref={root}>
      {pending && arrival.waiting && (
        <span
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
          aria-live="polite"
          data-testid="period-status"
        >
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          Building {formatPeriod(pending.range.start, pending.range.end)}…
        </span>
      )}
      {shownError && !open && (
        <p className="text-xs text-destructive" role="alert" data-testid="period-error">
          {shownError}
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-8 gap-1.5 px-2.5 text-xs tabular-nums"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => (open ? setOpen(false) : openPanel())}
        data-testid="period-trigger"
      >
        <CalendarRange className="size-3.5" aria-hidden />
        {label}
      </Button>
      {open && (
        <form
          id={panelId}
          role="dialog"
          aria-label="Pick a period"
          className="absolute right-0 top-full z-30 mt-1 flex w-[min(20rem,calc(100vw-2rem))] flex-col gap-3 rounded-lg border border-border bg-popover p-3 text-popover-foreground shadow-lg"
          onSubmit={(event) => {
            event.preventDefault();
            void apply();
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
            }
          }}
        >
          <div role="radiogroup" aria-label="Period length" className="grid grid-cols-4 gap-1 rounded-md bg-muted p-0.5">
            {GRAINS.map((g) => (
              <button
                key={g.id}
                type="button"
                role="radio"
                aria-checked={grain === g.id}
                onClick={() => {
                  setGrain(g.id);
                  setError(null);
                }}
                className={`rounded px-1.5 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-ring ${
                  grain === g.id ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
                }`}
                data-testid={`period-grain-${g.id}`}
              >
                {g.label}
              </button>
            ))}
          </div>
          {grain === "custom" ? (
            <div className="grid grid-cols-2 gap-2">
              <DateField label="From" value={start} onChange={setDate(setStart)} max={lastEnded} testId="period-start" errorId={panelError ? errorId : undefined} />
              <DateField label="To" value={end} onChange={setDate(setEnd)} max={lastEnded} testId="period-end" errorId={panelError ? errorId : undefined} />
            </div>
          ) : (
            <DateField
              label={grain === "day" ? "Day" : `Any day in the ${grain}`}
              value={anchor}
              onChange={setDate(setAnchor)}
              max={lastEnded}
              testId="period-anchor"
              errorId={panelError ? errorId : undefined}
            />
          )}
          {panelError && (
            <p id={errorId} className="text-xs text-destructive" role="alert" data-testid="period-error">
              {panelError}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={sending || unfinished !== null} data-testid="period-apply">
              {sending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
              Show period
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}

/** Why `range` cannot be asked for yet, in words, or null when it has ended. */
function unfinishedText(range: PeriodRange, today: string): string | null {
  if (isFuturePeriod(range, today)) {
    return "That period hasn't happened yet, so there is nothing to report on.";
  }
  if (!isUnfinishedPeriod(range, today)) return null;
  const what = range.grain === "custom" ? "range" : range.grain;
  return `That ${what} is not over yet: it ends ${formatPeriod(range.end, range.end)}. Pick one that has ended.`;
}

function DateField({
  label,
  value,
  onChange,
  max,
  testId,
  errorId,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  max?: string;
  testId: string;
  errorId?: string;
}) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[11px] font-medium text-muted-foreground">
        {label}
      </label>
      <input
        id={id}
        type="date"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        max={max}
        aria-describedby={errorId}
        aria-invalid={errorId ? true : undefined}
        className="h-8 rounded-md border border-input bg-card px-2 text-sm text-foreground focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-1"
        data-testid={testId}
      />
    </div>
  );
}
