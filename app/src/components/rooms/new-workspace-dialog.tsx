"use client";

import { useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { NativeSelect } from "@/components/ui/native-select";
import { useAgents } from "@/hooks/use-agents";
import { createRoom } from "@/lib/rooms/api";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import { useEnterRoom } from "./open-as-workspace";

export const CANNOT_CREATE_MESSAGE = `Your role can't create ${ROOM_LABEL.plural.toLowerCase()}. Ask a company admin if you need one.`;

/**
 * The bridge middleware's scope refusal, `{error: "insufficient scope"}` at
 * the top level: the caller's role (a viewer) cannot write here at all. It
 * answers before the workspace routes, so it has none of their `detail`.
 */
export function isScopeRefusal(status: number, body: unknown): boolean {
  if (status !== 403 || !body || typeof body !== "object") return false;
  const { error, detail } = body as { error?: unknown; detail?: unknown };
  return detail === undefined && error === "insufficient scope";
}

/**
 * Asks for the agent and a title, then `POST /api/rooms` (kind `agent`). A
 * refusal (unknown or disallowed agent) stays in the dialog in the server's words.
 */
export function NewWorkspaceDialog({
  initialAgentId,
  initialTitle,
  onClose,
}: {
  initialAgentId?: string;
  initialTitle: string;
  onClose: () => void;
}) {
  const { agents } = useAgents();
  const enter = useEnterRoom();
  const choices = agents.filter((a): a is typeof a & { agentId: string } => Boolean(a.agentId));
  const fallback = choices.find((a) => a.agentId === "main")?.agentId ?? choices[0]?.agentId ?? "";
  const [agentPicked, setAgentPicked] = useState<string | null>(initialAgentId ?? null);
  const agentId = agentPicked ?? fallback;
  const [title, setTitle] = useState(initialTitle);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The caller's role cannot create workspaces at all: nothing to retry.
  const [cannotCreate, setCannotCreate] = useState(false);
  // An agent passed in that the health list does not name is still offered.
  const options =
    agentId && !choices.some((a) => a.agentId === agentId)
      ? [{ agentId, name: agentId }, ...choices]
      : choices;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!title.trim() || !agentId) return;
    setBusy(true);
    setError(null);
    try {
      const result = await createRoom({ kind: "agent", title: title.trim(), agentId });
      if (result.ok) {
        onClose();
        enter(result);
      } else if (isScopeRefusal(result.status, result.body)) {
        setError(CANNOT_CREATE_MESSAGE);
        setCannotCreate(true);
      } else {
        setError(result.message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-md" data-testid="new-workspace-dialog">
        <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-4">
          <DialogHeader>
            <DialogTitle>New {ROOM_LABEL.singular}</DialogTitle>
            <DialogDescription>
              A conversation of its own with one agent, kept beside your main conversation.
            </DialogDescription>
          </DialogHeader>
          <label className="flex flex-col gap-1.5 text-xs font-medium text-foreground">
            Agent
            <NativeSelect
              value={agentId}
              onChange={(e) => setAgentPicked(e.target.value)}
              wrapperClassName="w-full"
              className="w-full"
              data-testid="new-workspace-agent"
            >
              {options.length === 0 && <option value="">No agents available</option>}
              {options.map((a) => (
                <option key={a.agentId} value={a.agentId}>
                  {a.name}
                </option>
              ))}
            </NativeSelect>
          </label>
          <label className="flex flex-col gap-1.5 text-xs font-medium text-foreground">
            Title
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="What is this conversation for?"
              maxLength={200}
              autoFocus
              data-testid="new-workspace-title"
            />
          </label>
          {error && (
            <p className="text-xs text-destructive" role="alert" data-testid="new-workspace-error">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              disabled={busy || cannotCreate || !title.trim() || !agentId}
              data-testid="new-workspace-create"
            >
              {busy && <Loader2 className="animate-spin" aria-hidden />}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
