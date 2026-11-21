"use client";

import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert, UserMinus, UserPlus, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect } from "@/components/ui/native-select";
import { refusalText } from "@/components/rooms/controls-shared";
import { refusalOf } from "@/lib/query/fetch-refused";
import { useAppQueryClient } from "@/lib/query/query-client";
import { deleteMember, fetchPeople, putMember } from "@/lib/rooms/api";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import type { Room } from "@/lib/rooms/types";

export const PEOPLE_QUERY_KEY = ["rooms", "people"] as const;

/**
 * Who a workspace is shared with. Opened only by someone who may share it
 * (assigned users and admins); a shared user never sees it, so cannot share
 * onward. It says who the workspace runs as, and warns in plain words that
 * the people it is shared with see data fetched with that person's access.
 * The bridge refuses anyone outside this company.
 */
export function ShareDialog({
  room,
  open,
  onOpenChange,
  onChanged,
}: {
  room: Room;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}) {
  const client = useAppQueryClient();
  const people = useQuery(
    { queryKey: PEOPLE_QUERY_KEY, queryFn: ({ signal }) => fetchPeople(signal), enabled: open, staleTime: 60_000 },
    client,
  );
  const members = useMemo(() => (Array.isArray(room.members) ? room.members : []), [room.members]);
  const candidates = useMemo(
    () => (people.data ?? []).filter((p) => !members.some((m) => m.userId === p.userId)),
    [people.data, members],
  );
  const [chosen, setChosen] = useState("");
  const [assignNew, setAssignNew] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectId = useId();
  const runsAs = room.runsAs?.displayName;

  const act = async (key: string, fn: () => Promise<unknown>, doing: string, after?: () => void) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
      after?.();
      onChanged();
    } catch (err) {
      setError(refusalText(err, doing));
    } finally {
      setBusy(null);
    }
  };

  const refused = refusalOf(people.error);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg" data-testid="share-dialog">
        <DialogHeader>
          <DialogTitle>Share “{room.title}”</DialogTitle>
          <DialogDescription>
            People in this company only. Assigned people can change its settings and share it; everyone else can read,
            chat and run it.
          </DialogDescription>
        </DialogHeader>

        {runsAs && (
          <div className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
            <p className="inline-flex items-center gap-1.5 font-medium text-foreground" data-testid="share-runs-as">
              <UserRound className="size-4" aria-hidden />
              Runs as {runsAs}
            </p>
            <p className="mt-1 flex gap-1.5 text-xs leading-relaxed text-muted-foreground" data-testid="share-warning">
              <ShieldAlert className="mt-px size-3.5 shrink-0 text-warning" aria-hidden />
              <span>
                People you share this with see data fetched with {runsAs}&apos;s access, including anything {runsAs} can
                see that they can&apos;t.
              </span>
            </p>
          </div>
        )}

        <section aria-label="People with access">
          <h3 className="pb-1.5 text-[10.5px] font-medium uppercase tracking-[0.09em] text-muted-foreground/70">
            People with access
          </h3>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {members.map((member) => (
              <li
                key={member.userId}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2"
                data-testid={`share-member-${member.userId}`}
              >
                <span className="min-w-0 flex-1 truncate text-sm text-foreground">{member.displayName}</span>
                <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={member.assigned}
                    disabled={busy !== null}
                    onChange={() =>
                      void act(member.userId, () => putMember(room.id, member.userId, { assigned: !member.assigned }), "change that")
                    }
                    className="size-3.5 accent-primary"
                    data-testid={`share-assigned-${member.userId}`}
                  />
                  Assigned
                </label>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Remove ${member.displayName}`}
                  title={`Remove ${member.displayName}`}
                  disabled={busy !== null}
                  onClick={() => void act(member.userId, () => deleteMember(room.id, member.userId), "remove them")}
                  data-testid={`share-remove-${member.userId}`}
                >
                  <UserMinus aria-hidden />
                </Button>
              </li>
            ))}
            {members.length === 0 && <li className="px-3 py-2 text-sm text-muted-foreground">Nobody yet.</li>}
          </ul>
        </section>

        <section aria-label="Add a person" className="flex flex-col gap-2">
          <label htmlFor={selectId} className="text-xs font-medium text-foreground">
            Add a person
          </label>
          {people.isError ? (
            <p className="text-xs leading-relaxed text-muted-foreground" data-testid="share-no-directory">
              {refused?.status === 403
                ? `Your role can't browse the company's people here. Ask the agent to share this ${ROOM_LABEL.singular} by name.`
                : `The list of people could not be read${refused?.status ? ` (the server answered ${refused.status})` : ""}. You can also ask the agent to share it by name.`}
            </p>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <NativeSelect
                id={selectId}
                value={chosen}
                onChange={(e) => setChosen(e.target.value)}
                wrapperClassName="min-w-0 basis-full sm:basis-auto sm:flex-1"
                className="w-full"
                data-testid="share-person"
              >
                <option value="">{people.isPending ? "Loading people…" : "Choose a person…"}</option>
                {candidates.map((person) => (
                  <option key={person.userId} value={person.userId}>
                    {person.displayName}
                  </option>
                ))}
              </NativeSelect>
              <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={assignNew}
                  onChange={(e) => setAssignNew(e.target.checked)}
                  className="size-3.5 accent-primary"
                  data-testid="share-add-assigned"
                />
                Assigned
              </label>
              <Button
                type="button"
                size="sm"
                disabled={!chosen || busy !== null}
                onClick={() =>
                  void act("add", () => putMember(room.id, chosen, { assigned: assignNew }), "share it", () => {
                    setChosen("");
                    setAssignNew(false);
                  })
                }
                data-testid="share-add"
              >
                <UserPlus className="size-3.5" aria-hidden />
                Share
              </Button>
            </div>
          )}
        </section>

        {error && (
          <p className="text-sm text-destructive" role="alert" data-testid="share-error">
            {error}
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
