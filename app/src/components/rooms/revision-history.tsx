"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { History, Loader2, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { refusalText } from "@/components/rooms/controls-shared";
import { refusalOf } from "@/lib/query/fetch-refused";
import { useAppQueryClient } from "@/lib/query/query-client";
import { fetchRevisions, restoreRevision, type Revision } from "@/lib/rooms/api";
import { formatWhen } from "@/lib/rooms/format";

/** The parts of a definition a person would recognise, in the order shown. */
const FIELDS: Array<{ label: string; read: (d: Record<string, unknown>) => unknown }> = [
  { label: "Title", read: (d) => d.title },
  { label: "Instructions", read: (d) => d.instructions },
  { label: "Schedule", read: (d) => d.schedule ?? settingsOf(d).schedule_text ?? settingsOf(d).scheduleText },
  { label: "Time zone", read: (d) => settingsOf(d).tz },
  { label: "Default period", read: (d) => settingsOf(d).default_grain ?? settingsOf(d).defaultGrain },
  { label: "Notifications", read: (d) => settingsOf(d).notify_default ?? settingsOf(d).notifyDefault },
  { label: "Paused", read: (d) => d.paused ?? settingsOf(d).paused },
];

function settingsOf(definition: Record<string, unknown>): Record<string, unknown> {
  const s = definition?.settings;
  return s && typeof s === "object" ? (s as Record<string, unknown>) : {};
}

function shown(value: unknown): string {
  if (value === undefined || value === null || value === "") return "(none)";
  if (typeof value === "boolean") return value ? "on" : "off";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export interface Change {
  label: string;
  before: string;
  after: string;
}

/** What one revision changed from the one before it. */
export function diffRevisions(before: Revision | undefined, after: Revision): Change[] {
  if (!before) return [];
  const changes: Change[] = [];
  for (const field of FIELDS) {
    const a = field.read(before.definition ?? {});
    const b = field.read(after.definition ?? {});
    if (shown(a) !== shown(b)) changes.push({ label: field.label, before: shown(a), after: shown(b) });
  }
  return changes;
}

/**
 * Every revision of a workspace's definition: who, when and what changed,
 * newest first. Restore writes a new revision equal to the chosen one; it is
 * offered only to people who may change the definition (assigned users and
 * admins), and never for the revision in force.
 */
export function RevisionHistory({
  roomId,
  currentRev,
  canRestore,
  onRestored,
}: {
  roomId: string;
  currentRev: number;
  canRestore: boolean;
  onRestored: () => void;
}) {
  const client = useAppQueryClient();
  const query = useQuery(
    {
      queryKey: ["rooms", roomId, "revisions", currentRev],
      queryFn: ({ signal }) => fetchRevisions(roomId, signal),
      staleTime: 15_000,
    },
    client,
  );
  const [restoring, setRestoring] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (query.isPending) {
    return (
      <p className="inline-flex items-center gap-2 text-sm text-muted-foreground" aria-busy="true">
        <Loader2 className="size-4 animate-spin" aria-hidden />
        Loading history…
      </p>
    );
  }
  const refused = refusalOf(query.error);
  if (query.isError) {
    return (
      <p className="text-sm text-muted-foreground" role="alert" data-testid="revisions-error">
        The history could not be read{refused?.status ? ` (the server answered ${refused.status})` : ""}.
      </p>
    );
  }
  const revisions = query.data ?? [];
  if (revisions.length === 0) {
    return <p className="text-sm text-muted-foreground">No revisions recorded yet.</p>;
  }
  const ascending = [...revisions].sort((a, b) => a.rev - b.rev);

  const restore = async (rev: number) => {
    setRestoring(rev);
    setError(null);
    try {
      await restoreRevision(roomId, rev);
      void client.invalidateQueries({ queryKey: ["rooms", roomId, "revisions"] });
      onRestored();
    } catch (err) {
      setError(refusalText(err, `restore revision ${rev}`));
    } finally {
      setRestoring(null);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      {error && (
        <p className="text-sm text-destructive" role="alert" data-testid="revisions-restore-error">
          {error}
        </p>
      )}
      <ol className="flex flex-col gap-2" aria-label="Revisions">
        {revisions.map((revision) => {
          const previous = ascending.filter((r) => r.rev < revision.rev).at(-1);
          const changes = diffRevisions(previous, revision);
          const who = revision.changedByName || revision.changedBy || "Someone";
          return (
            <li
              key={revision.rev}
              className="rounded-lg border border-border p-3"
              data-testid={`revision-${revision.rev}`}
            >
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="inline-flex items-center gap-1 text-xs font-medium text-foreground">
                  <History className="size-3.5 text-muted-foreground" aria-hidden />
                  Revision {revision.rev}
                </span>
                {revision.rev === currentRev && (
                  <span className="rounded-full border border-primary/25 bg-primary/10 px-1.5 py-px text-[10px] font-medium text-primary">
                    In force
                  </span>
                )}
                <span className="text-xs text-muted-foreground">
                  {who} · {formatWhen(revision.changedAt) || "unknown time"}
                </span>
                {canRestore && revision.rev !== currentRev && (
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    className="ml-auto"
                    disabled={restoring !== null}
                    onClick={() => void restore(revision.rev)}
                    aria-label={`Restore revision ${revision.rev}`}
                    data-testid={`revision-restore-${revision.rev}`}
                  >
                    <RotateCcw aria-hidden />
                    Restore
                  </Button>
                )}
              </div>
              {previous ? (
                changes.length > 0 ? (
                  <ul className="mt-2 flex flex-col gap-1.5">
                    {changes.map((change) => (
                      <li key={change.label} className="text-xs">
                        <span className="font-medium text-foreground">{change.label}</span>
                        <span className="mt-0.5 block break-words text-muted-foreground">
                          <del className="decoration-destructive/60">{change.before}</del>
                          {" → "}
                          <ins className="no-underline text-foreground">{change.after}</ins>
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-2 text-xs text-muted-foreground">No visible change (a restore or a re-save).</p>
                )
              ) : (
                <p className="mt-2 text-xs text-muted-foreground">Created.</p>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
