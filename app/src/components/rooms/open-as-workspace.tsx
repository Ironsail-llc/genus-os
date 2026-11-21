"use client";

import { useState } from "react";
import dynamic from "next/dynamic";
import { Loader2, Plus, SquareArrowOutUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ROOMS_QUERY_KEY, useRooms } from "@/hooks/use-rooms";
import { openTaskRoom, type RoomOpened } from "@/lib/rooms/api";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import { navigateTo } from "@/lib/rooms/navigate";
import { roomLink } from "@/lib/rooms/room-id";
import { useAppQueryClient } from "@/lib/query/query-client";

// The dialog (form, agent list) is fetched the first time someone opens it.
const NewWorkspaceDialog = dynamic(() => import("./new-workspace-dialog").then((m) => m.NewWorkspaceDialog), {
  ssr: false,
});

/**
 * Whether to offer workspaces at all: only once the rooms list has answered,
 * and never where it answers 404 (nothing serves workspaces here, the same
 * rule that hides the shell's rail). Reads the shared rooms cache.
 */
export function useWorkspacesServed(): boolean {
  const { available, isLoading } = useRooms();
  return available && !isLoading;
}

/** After a room was made or found: the rail lists it, and the room opens. */
export function useEnterRoom() {
  const client = useAppQueryClient();
  return (result: RoomOpened) => {
    if (!result.ok) return;
    void client.invalidateQueries({ queryKey: ROOMS_QUERY_KEY });
    navigateTo(roomLink(result.room.id));
  };
}

/**
 * "Open as workspace" on a task: `POST /api/rooms/from-task/{task_id}` makes
 * the task's workspace or hands back the one that exists, so a second person
 * opening the same task lands in the same place. Then into it, in place.
 */
export function OpenAsWorkspace({
  taskId,
  label,
  assumeServed = false,
}: {
  taskId: string;
  /** The button's words; "Open as workspace" by default. */
  label?: string;
  /** Inside a workspace, workspaces are served: no need to ask. */
  assumeServed?: boolean;
}) {
  const served = useWorkspacesServed() || assumeServed;
  const enter = useEnterRoom();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function open() {
    setBusy(true);
    setError(null);
    try {
      const result = await openTaskRoom(taskId);
      if (result.ok) enter(result);
      else setError(result.message);
    } finally {
      setBusy(false);
    }
  }

  if (!served) return null;
  return (
    <div className="mt-2">
      <Button
        type="button"
        size="xs"
        variant="ghost"
        className="text-muted-foreground hover:text-foreground"
        disabled={busy}
        onClick={() => void open()}
        data-testid={`open-as-workspace-${taskId}`}
      >
        {busy ? <Loader2 className="animate-spin" aria-hidden /> : <SquareArrowOutUpRight aria-hidden />}
        {label ?? `Open as ${ROOM_LABEL.singular}`}
      </Button>
      {error && (
        <p className="mt-1 text-xs text-destructive" role="alert" data-testid={`open-as-workspace-error-${taskId}`}>
          {error}
        </p>
      )}
    </div>
  );
}

export interface NewWorkspaceButtonProps {
  /** The agent to start with (from the Agents view); otherwise the main one, or the first listed. */
  agentId?: string;
  /** Its display name, for the default title. */
  agentName?: string;
  label?: string;
  /** "rail" draws it as the rail's own nav row; "inline" as a small button. */
  variant?: "rail" | "inline";
}

/**
 * "New workspace": a conversation with an agent of its own (kind `agent`).
 * Asks for the agent and a title first, then `POST /api/rooms`; a refusal
 * (unknown or disallowed agent) is shown in the dialog in the server's words.
 */
export function NewWorkspaceButton({
  agentId,
  agentName,
  label = `New ${ROOM_LABEL.singular}`,
  variant = "inline",
}: NewWorkspaceButtonProps) {
  const served = useWorkspacesServed();
  const [open, setOpen] = useState(false);
  if (!served) return null;
  return (
    <>
      {variant === "rail" ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex w-full items-center gap-2.5 rounded-md border border-dashed border-border px-2 py-1.5 text-left text-[13px] text-muted-foreground transition-colors hover:border-primary/30 hover:bg-muted/60 hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          data-testid="new-workspace-button"
        >
          <Plus className="size-4 shrink-0" aria-hidden />
          <span className="truncate">{label}</span>
        </button>
      ) : (
        <Button
          type="button"
          size="xs"
          variant="outline"
          onClick={() => setOpen(true)}
          data-testid="new-workspace-button"
        >
          <Plus aria-hidden />
          {label}
        </Button>
      )}
      {open && (
        <NewWorkspaceDialog
          initialAgentId={agentId}
          initialTitle={agentName ?? `New ${ROOM_LABEL.singular}`}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}
