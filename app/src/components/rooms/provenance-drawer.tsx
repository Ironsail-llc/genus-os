"use client";

import { CheckCircle2, XCircle } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { formatPeriod, formatWhen } from "@/lib/rooms/format";
import type { Edition, RoomMember } from "@/lib/rooms/types";

interface ToolCall {
  tool: string;
  at: string | null;
  ok: boolean | null;
}

interface Provenance {
  runId: string | null;
  ranAs: string | null;
  startedAt: string | null;
  toolCalls: ToolCall[];
}

const str = (v: unknown) => (typeof v === "string" && v ? v : null);

/** The provenance the server recorded, in either case style; null when there is none. */
export function readProvenance(edition: Edition, members: RoomMember[]): Provenance | null {
  const p = edition.provenance;
  if (!p || typeof p !== "object") return null;
  const rawRanAs = p.ran_as ?? p.ranAs;
  let ranAs: string | null = null;
  if (typeof rawRanAs === "string") {
    ranAs = members.find((m) => m.userId === rawRanAs)?.displayName ?? rawRanAs;
  } else if (rawRanAs && typeof rawRanAs === "object") {
    const o = rawRanAs as Record<string, unknown>;
    const id = str(o.userId) ?? str(o.user_id);
    ranAs = str(o.displayName) ?? str(o.display_name) ?? members.find((m) => m.userId === id)?.displayName ?? id;
  }
  const calls = p.tool_calls ?? p.toolCalls;
  return {
    runId: str(p.run_id) ?? str(p.runId),
    ranAs,
    startedAt: str(p.started_at) ?? str(p.startedAt),
    toolCalls: Array.isArray(calls)
      ? calls
          .filter((c): c is Record<string, unknown> => !!c && typeof c === "object")
          .map((c) => ({ tool: str(c.tool) ?? "unknown tool", at: str(c.at), ok: typeof c.ok === "boolean" ? c.ok : null }))
      : [],
  };
}

/**
 * Where an edition's figures came from: the run, when it ran, whose
 * permissions it ran with, and every tool call it made. All of it is what
 * the server recorded from the run; nothing the agent wrote is shown here.
 */
export function ProvenanceDrawer({
  edition,
  members,
  open,
  onOpenChange,
}: {
  edition: Edition;
  members: RoomMember[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const p = readProvenance(edition, members);
  const period = formatPeriod(edition.periodStart, edition.periodEnd);
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-md" data-testid="provenance-drawer">
        <SheetHeader className="border-b border-border pr-10">
          <SheetTitle>How this edition was made</SheetTitle>
          <SheetDescription>
            {edition.title || "Untitled edition"}
            {period ? ` · ${period}` : ""}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-4 p-4">
          {!p ? (
            <p className="text-sm text-muted-foreground" data-testid="provenance-missing">
              No run was recorded for this edition, so its figures cannot be traced.
            </p>
          ) : (
            <>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
                <dt className="text-muted-foreground">Run</dt>
                <dd className="min-w-0 break-all font-mono text-xs text-foreground" data-testid="provenance-run">
                  {p.runId ?? "not recorded"}
                </dd>
                <dt className="text-muted-foreground">When</dt>
                <dd className="text-foreground" data-testid="provenance-when">
                  {formatWhen(p.startedAt) || "not recorded"}
                </dd>
                <dt className="text-muted-foreground">Ran as</dt>
                <dd className="text-foreground" data-testid="provenance-ran-as">
                  {p.ranAs ?? "not recorded"}
                </dd>
              </dl>
              <section aria-labelledby="provenance-tools-label">
                <h3
                  id="provenance-tools-label"
                  className="pb-1.5 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70"
                >
                  Tool calls
                </h3>
                {p.toolCalls.length === 0 ? (
                  <p className="text-sm text-muted-foreground" data-testid="provenance-tools-empty">
                    No tool calls were recorded.
                  </p>
                ) : (
                  <ol className="divide-y divide-border rounded-lg border border-border">
                    {p.toolCalls.map((call, i) => (
                      <li key={i} className="flex items-center gap-2 px-3 py-2 text-sm" data-testid={`provenance-tool-${i}`}>
                        {call.ok === false ? (
                          <XCircle className="size-4 shrink-0 text-destructive" aria-hidden />
                        ) : (
                          <CheckCircle2 className="size-4 shrink-0 text-success" aria-hidden />
                        )}
                        <code className="min-w-0 flex-1 truncate font-mono text-xs text-foreground">{call.tool}</code>
                        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{formatWhen(call.at)}</span>
                        <span className={`shrink-0 text-xs ${call.ok === false ? "text-destructive" : "text-muted-foreground"}`}>
                          {call.ok === false ? "Failed" : call.ok === true ? "OK" : "Unknown"}
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            </>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground">
            Recorded by the server from the run itself. Nothing here is written by the agent.
          </p>
        </div>
      </SheetContent>
    </Sheet>
  );
}
