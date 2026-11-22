"use client";

import { useId, useState } from "react";
import { useSession } from "next-auth/react";
import { useQuery } from "@tanstack/react-query";
import { Archive, ArchiveRestore, Building2, PauseCircle, UserCog } from "lucide-react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { refusalText } from "@/components/rooms/controls-shared";
import { PEOPLE_QUERY_KEY } from "@/components/rooms/share-dialog";
import { ROOMS_QUERY_KEY } from "@/hooks/use-rooms";
import { refusalOf } from "@/lib/query/fetch-refused";
import { useAppQueryClient } from "@/lib/query/query-client";
import { fetchAllRooms, fetchPeople, reassignRoom, unarchiveRoom, type AdminRoomRow } from "@/lib/rooms/api";
import { ROOM_KIND_LABELS, ROOM_LABEL } from "@/lib/rooms/labels";

export const ALL_ROOMS_QUERY_KEY = ["rooms", "all"] as const;
const ADMIN_ROLES = new Set(["owner", "admin"]);
const LIST_LABEL = `All company ${ROOM_LABEL.plural.toLowerCase()}`;

/**
 * The rail's way in to every workspace in the company, for owners and
 * admins only (the session's role; the bridge checks again). Nothing is
 * fetched until it is opened.
 */
export function AllRoomsAdminEntry({ onOpenRoom }: { onOpenRoom: (roomId: string) => void }) {
  const { data: session } = useSession();
  const [open, setOpen] = useState(false);
  if (!ADMIN_ROLES.has(String(session?.role ?? ""))) return null;
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-3 flex w-full items-center gap-2.5 rounded-md border border-transparent px-2 py-1.5 text-left text-[13px] text-foreground/70 transition-colors hover:bg-muted/60 hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        data-testid="rooms-admin-entry"
      >
        <Building2 className="size-4 shrink-0" aria-hidden />
        <span className="min-w-0 flex-1 truncate">{LIST_LABEL}</span>
      </button>
      {open && (
        <AllRoomsAdmin
          open
          onOpenChange={setOpen}
          onOpenRoom={(roomId) => {
            setOpen(false);
            onOpenRoom(roomId);
          }}
        />
      )}
    </>
  );
}

/**
 * Every workspace in the company (`GET /api/rooms?scope=all`) with its
 * assigned people and whether it is paused or archived; an admin reassigns
 * one (the new person becomes assigned and the one it runs as) or restores
 * an archived one, which comes back paused. No workspace is orphaned: one
 * with nobody assigned says so.
 */
export function AllRoomsAdmin({
  open,
  onOpenChange,
  onOpenRoom,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onOpenRoom: (roomId: string) => void;
}) {
  const client = useAppQueryClient();
  const query = useQuery(
    { queryKey: ALL_ROOMS_QUERY_KEY, queryFn: ({ signal }) => fetchAllRooms(signal), enabled: open, staleTime: 15_000 },
    client,
  );
  const refused = refusalOf(query.error);
  const rows = query.data ?? [];

  const reload = () => {
    void client.invalidateQueries({ queryKey: ALL_ROOMS_QUERY_KEY });
    void client.invalidateQueries({ queryKey: ROOMS_QUERY_KEY });
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto sm:max-w-xl" data-testid="rooms-admin">
        <SheetHeader className="border-b border-border pr-10">
          <SheetTitle>{LIST_LABEL}</SheetTitle>
          <SheetDescription>Every {ROOM_LABEL.singular} in this company, whoever made it.</SheetDescription>
        </SheetHeader>
        <div className="p-4">
          {query.isPending && open ? (
            <div className="flex flex-col gap-2" aria-busy="true" aria-label="Loading">
              <div className="h-12 animate-pulse rounded-lg bg-muted" />
              <div className="h-12 animate-pulse rounded-lg bg-muted/70" />
            </div>
          ) : query.isError ? (
            <p className="text-sm text-muted-foreground" role="alert" data-testid="rooms-admin-error">
              The list could not be read{refused?.status ? ` (the server answered ${refused.status})` : ""}.
            </p>
          ) : rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">No {ROOM_LABEL.plural.toLowerCase()} in this company yet.</p>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border">
              {rows.map((row) => (
                <AdminRow key={row.id} row={row} onOpen={() => onOpenRoom(row.id)} onChanged={reload} />
              ))}
            </ul>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}

/**
 * Who is assigned, from the row's `assigned` list or its `members`; null when
 * the row carries neither, so the list never claims nobody is assigned when
 * it simply was not told.
 */
function assignedPeople(row: AdminRoomRow): Array<{ userId: string; displayName: string }> | null {
  if (Array.isArray(row.assigned)) return row.assigned;
  if (Array.isArray(row.members)) return row.members.filter((m) => m.assigned);
  return null;
}

function AdminRow({ row, onOpen, onChanged }: { row: AdminRoomRow; onOpen: () => void; onChanged: () => void }) {
  const client = useAppQueryClient();
  const [reassigning, setReassigning] = useState(false);
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const restoreHint = useId();
  const people = useQuery(
    { queryKey: PEOPLE_QUERY_KEY, queryFn: ({ signal }) => fetchPeople(signal), enabled: reassigning, staleTime: 60_000 },
    client,
  );
  const assigned = assignedPeople(row);

  const act = async (fn: () => Promise<unknown>, doing: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setReassigning(false);
      setTo("");
      onChanged();
    } catch (err) {
      setError(refusalText(err, doing));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex flex-col gap-2 px-3 py-2.5" data-testid={`rooms-admin-row-${row.id}`}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium text-foreground">{row.title}</p>
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
            <span>{ROOM_KIND_LABELS[row.kind] ?? ROOM_LABEL.title}</span>
            <span aria-hidden>·</span>
            {assigned === null ? (
              <span>Assigned: unknown</span>
            ) : assigned.length > 0 ? (
              <span>Assigned: {assigned.map((m) => m.displayName).join(", ")}</span>
            ) : (
              <span className="text-warning">Nobody assigned: reassign it</span>
            )}
            {row.paused && !row.archived && (
              <span className="inline-flex items-center gap-0.5">
                <PauseCircle className="size-3" aria-hidden />
                Paused
              </span>
            )}
            {row.archived && (
              <span className="inline-flex items-center gap-0.5">
                <Archive className="size-3" aria-hidden />
                Archived
              </span>
            )}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <Button type="button" variant="outline" size="xs" onClick={onOpen}>
            Open
          </Button>
          <Button
            type="button"
            variant="outline"
            size="xs"
            aria-expanded={reassigning}
            onClick={() => setReassigning((v) => !v)}
            data-testid={`rooms-admin-reassign-${row.id}`}
          >
            <UserCog aria-hidden />
            Reassign
          </Button>
          {row.archived && (
            <>
              <Button
                type="button"
                variant="outline"
                size="xs"
                disabled={busy}
                aria-describedby={restoreHint}
                onClick={() => void act(() => unarchiveRoom(row.id), "restore it")}
                data-testid={`rooms-admin-restore-${row.id}`}
              >
                <ArchiveRestore aria-hidden />
                Restore
              </Button>
              <span id={restoreHint} className="sr-only">
                It comes back paused; resume it from its settings.
              </span>
            </>
          )}
        </div>
      </div>
      {reassigning && (
        <div className="flex flex-wrap items-center gap-2">
          {people.isError ? (
            <p className="text-xs text-muted-foreground">The list of people could not be read.</p>
          ) : (
            <>
              <NativeSelect
                aria-label={`Reassign ${row.title} to`}
                value={to}
                onChange={(e) => setTo(e.target.value)}
                wrapperClassName="min-w-0 flex-1"
                className="w-full"
                data-testid={`rooms-admin-reassign-to-${row.id}`}
              >
                <option value="">{people.isPending ? "Loading people…" : "Choose a person…"}</option>
                {(people.data ?? []).map((person) => (
                  <option key={person.userId} value={person.userId}>
                    {person.displayName}
                  </option>
                ))}
              </NativeSelect>
              <Button
                type="button"
                size="xs"
                disabled={!to || busy}
                onClick={() => void act(() => reassignRoom(row.id, to), "reassign it")}
                data-testid={`rooms-admin-reassign-confirm-${row.id}`}
              >
                Reassign
              </Button>
            </>
          )}
          <p className="w-full text-xs text-muted-foreground">
            They become assigned, and scheduled runs use their permissions from now on.
          </p>
        </div>
      )}
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}
