"use client";

/**
 * What the agent in this chat can reach: its tools, grouped by where each one
 * comes from, and the MCP servers with their health.
 *
 * Read-only on purpose. Which tools an agent gets is decided by its manifest
 * (`tools_allowed`, edited in the agent editor; `tools_denied`, edited in the
 * manifest file), so this dialog shows the answer and links to where it is
 * changed rather than growing a second place to change it.
 *
 * A `Dialog` on desktop and a bottom `Sheet` on mobile, both from `ui/`: there
 * is no popover primitive in this app, and one dialog is not worth a new
 * dependency.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronRight, Loader2, Wrench } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

const BRIDGE = "/api/bridge";
export const TOOLS_LABEL = "Tools and MCP servers";
const TITLE = "Tools";
const DESCRIPTION = "What this agent can call, and the MCP servers behind it.";

export interface AgentToolsReply {
  agent: string;
  groups: {
    core: string[];
    plugins: Record<string, string[]>;
    mcp: Record<string, string[]>;
    opt_in: string[];
  };
  allowed: string[];
  denied: string[];
  count: number;
}

export interface McpServer {
  name: string;
  transport: string;
  running: boolean;
  initialized: boolean;
  protocol?: string;
  tool_count: number;
}

type Load =
  | { state: "idle" }
  | { state: "refused" }
  | { state: "error"; message: string }
  | { state: "signed-out" }
  | { state: "ready"; tools: AgentToolsReply; servers: McpServer[] };

/** Where the agent editor opens for one agent (`agent-manifests.tsx`). */
export function agentEditorHref(agentId: string): string {
  return `/?v=agents&agent=${encodeURIComponent(agentId)}`;
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function health(server: McpServer): { label: string; tone: string } {
  if (server.running && server.initialized) return { label: "Ready", tone: "text-success" };
  if (server.running) return { label: "Starting", tone: "text-warning" };
  return { label: "Stopped", tone: "text-muted-foreground" };
}

/**
 * One retry on 401, as the Flags page does: the first 401 is often the request
 * that lost a session-refresh race, and the retry carries the rotated cookie.
 */
async function readJson(url: string): Promise<{ status: number; body: unknown }> {
  let res = await fetch(url);
  if (res.status === 401) res = await fetch(url);
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  return { status: res.status, body };
}

function signInHref(): string {
  if (typeof window === "undefined") return "/signin";
  const back = `${window.location.pathname}${window.location.search}`;
  return `/signin?callbackUrl=${encodeURIComponent(back)}`;
}

/** True when *body* has the shape the dialog renders; anything else is an error. */
function isToolsReply(body: unknown): body is AgentToolsReply {
  if (!body || typeof body !== "object") return false;
  const reply = body as Partial<AgentToolsReply>;
  const groups = reply.groups;
  return (
    !!groups &&
    Array.isArray(groups.core) &&
    Array.isArray(groups.opt_in) &&
    !!groups.plugins &&
    typeof groups.plugins === "object" &&
    !!groups.mcp &&
    typeof groups.mcp === "object" &&
    Array.isArray(reply.allowed) &&
    Array.isArray(reply.denied) &&
    typeof reply.count === "number"
  );
}

/** The engine's own sentence from an error body, when it sent one. */
function errorText(body: unknown): string | null {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    for (const key of ["error", "detail", "message"]) {
      if (typeof record[key] === "string" && record[key]) return record[key] as string;
    }
  }
  return null;
}

function ToolGroup({ id, title, names }: { id: string; title: string; names: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <li data-testid={`tool-group-${id}`} className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted/50"
      >
        <ChevronRight className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
        <span className="flex-1 truncate">{title}</span>
        <span className="text-xs text-muted-foreground tabular-nums">{names.length}</span>
      </button>
      {open ? (
        <ul className="flex flex-wrap gap-1 border-t border-border px-3 py-2">
          {names.map((name) => (
            <li key={name} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
              {name}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function ToolsBody({
  agentId,
  load,
  listingRefused,
}: {
  agentId: string | null;
  load: Load;
  listingRefused: boolean;
}) {
  if (!agentId) {
    // Two different reasons for no agent id. A member's chat IS on the default
    // agent; the listing that would name it was refused for their role. A
    // listing that answered and named no default really leaves it unknown.
    return (
      <p className="text-sm text-muted-foreground" data-testid="tools-no-agent">
        {listingRefused
          ? "Only an operator can see which tools and servers an agent can reach."
          : "This chat has not been told which agent it is talking to, so there is nothing to list."}
      </p>
    );
  }
  if (load.state === "idle") {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground" data-testid="tools-loading">
        <Loader2 className="h-4 w-4 animate-spin" /> Asking the engine…
      </p>
    );
  }
  if (load.state === "refused") {
    return (
      <p className="text-sm text-muted-foreground" data-testid="tools-refused">
        Only an operator can see which tools and servers this agent can reach.
      </p>
    );
  }
  if (load.state === "signed-out") {
    return (
      <p className="text-sm text-muted-foreground" data-testid="tools-error">
        Your session has ended.{" "}
        <a href={signInHref()} className="font-medium text-primary underline-offset-4 hover:underline">
          Sign in again
        </a>{" "}
        to see this agent&apos;s tools.
      </p>
    );
  }
  if (load.state === "error") {
    return (
      <p className="text-sm text-destructive" data-testid="tools-error">
        {load.message}
      </p>
    );
  }

  const { tools, servers } = load;
  const groups: { id: string; title: string; names: string[] }[] = [
    { id: "core", title: "Core", names: tools.groups.core },
    ...Object.entries(tools.groups.plugins).map(([plugin, names]) => ({
      id: `plugin-${plugin}`,
      title: `Plugin: ${plugin}`,
      names,
    })),
    ...Object.entries(tools.groups.mcp).map(([server, names]) => ({
      id: `mcp-${server}`,
      title: `MCP: ${server}`,
      names,
    })),
    { id: "opt-in", title: "Opt-in", names: tools.groups.opt_in },
  ].filter((group) => group.names.length > 0);

  return (
    <div className="flex flex-col gap-4 overflow-y-auto">
      <section aria-labelledby="tools-mcp-heading" className="flex flex-col gap-2">
        <h3 id="tools-mcp-heading" className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          MCP servers
        </h3>
        {servers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No MCP servers are configured.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {servers.map((server) => {
              const { label, tone } = health(server);
              return (
                <li
                  key={server.name}
                  data-testid={`mcp-server-${server.name}`}
                  className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
                >
                  <span className="flex-1 truncate font-medium">{server.name}</span>
                  <span className={`text-xs ${tone}`}>{label}</span>
                  <span className="text-xs text-muted-foreground tabular-nums">
                    {plural(server.tool_count, "tool")}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section aria-labelledby="tools-groups-heading" className="flex flex-col gap-2">
        <h3
          id="tools-groups-heading"
          className="flex items-center justify-between text-xs font-semibold uppercase tracking-wide text-muted-foreground"
        >
          <span>Tools</span>
          <span data-testid="tools-total" className="normal-case tracking-normal">
            {plural(tools.count, "tool")}
          </span>
        </h3>
        <ul className="flex flex-col gap-1" data-testid="tools-groups">
          {groups.map((group) => (
            <ToolGroup key={group.id} {...group} />
          ))}
        </ul>
      </section>

      <section aria-labelledby="tools-policy-heading" className="flex flex-col gap-1 text-sm" data-testid="tools-policy">
        <h3 id="tools-policy-heading" className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Manifest
        </h3>
        <p>
          {tools.allowed.length > 0
            ? `${plural(tools.allowed.length, "tool")} allowed by name.`
            : "No allow list: every tool that is not opt-in."}
        </p>
        <p>
          {tools.denied.length > 0 ? (
            <>
              Denied: <span className="font-mono text-xs">{tools.denied.join(", ")}</span>
              <span className="block text-xs text-muted-foreground">
                The deny list is set in the manifest file.
              </span>
            </>
          ) : (
            "Nothing denied."
          )}
        </p>
      </section>

      <a
        href={agentEditorHref(agentId ?? tools.agent)}
        className="text-sm font-medium text-primary underline-offset-4 hover:underline"
      >
        Manage in agent settings
      </a>
    </div>
  );
}

export interface ToolsPopoverProps {
  /** The agent the chat is talking to, or null until the chat knows. */
  agentId: string | null;
  /** The fleet listing was refused for this session's role (a member). */
  listingRefused?: boolean;
  /** Labelled chip and bottom sheet instead of icon button and dialog. */
  mobile?: boolean;
  disabled?: boolean;
}

export function ToolsPopover({
  agentId,
  listingRefused = false,
  mobile = false,
  disabled = false,
}: ToolsPopoverProps) {
  const [open, setOpen] = useState(false);
  const [load, setLoad] = useState<Load>({ state: "idle" });

  // Each fetch takes a number; only the latest may write. Switching agent and
  // reopening while the first agent's reply is still in flight must not end
  // with that reply on screen under the second agent's name.
  const latest = useRef(0);

  const fetchAll = useCallback(async () => {
    if (!agentId) return;
    const ticket = ++latest.current;
    const settle = (next: Load) => {
      if (ticket === latest.current) setLoad(next);
    };
    try {
      const [tools, servers] = await Promise.all([
        readJson(`${BRIDGE}/api/agents/${encodeURIComponent(agentId)}/tools`),
        readJson(`${BRIDGE}/api/mcp/servers`),
      ]);
      if (tools.status === 401 || servers.status === 401) {
        settle({ state: "signed-out" });
        return;
      }
      if (tools.status === 403 || servers.status === 403) {
        settle({ state: "refused" });
        return;
      }
      if (tools.status === 404) {
        settle({ state: "error", message: `The engine has no agent called ${agentId}.` });
        return;
      }
      if (tools.status === 422) {
        settle({
          state: "error",
          message: errorText(tools.body) ?? "The engine refused this agent's manifest.",
        });
        return;
      }
      if (tools.status >= 400 || servers.status >= 400) {
        // The bridge's own words when it has any (an engine that refused the
        // bridge's credentials, say); its bare "engine unavailable" gets the
        // sentence an operator can act on.
        const failed = errorText(tools.status >= 400 ? tools.body : servers.body);
        settle({
          state: "error",
          message:
            failed && failed !== "engine unavailable"
              ? failed
              : "The engine did not answer. Check that it is running.",
        });
        return;
      }
      if (!isToolsReply(tools.body)) {
        settle({ state: "error", message: "The engine sent an unexpected answer about this agent's tools." });
        return;
      }
      const serverBody = servers.body as { servers?: McpServer[] } | null;
      settle({
        state: "ready",
        tools: tools.body,
        servers: serverBody?.servers ?? [],
      });
    } catch {
      settle({ state: "error", message: "The dashboard could not reach the bridge." });
    }
  }, [agentId]);

  // Asked when the dialog opens, and again if the chat learns (or changes) its
  // agent while it is open: the agent listing can answer after the click.
  useEffect(() => {
    if (open && agentId) void fetchAll();
  }, [open, agentId, fetchAll]);

  // Opening starts from the "asking" state, so a previous answer is never
  // shown as if it were this one.
  const onOpenChange = useCallback((next: boolean) => {
    if (next) setLoad({ state: "idle" });
    setOpen(next);
  }, []);

  if (mobile) {
    return (
      <>
        <button
          type="button"
          onClick={() => onOpenChange(true)}
          disabled={disabled}
          aria-label={TOOLS_LABEL}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors bg-muted/50 text-muted-foreground border border-transparent disabled:opacity-50"
          data-testid="tools-toggle"
        >
          <Wrench className="w-3.5 h-3.5" />
          Tools
        </button>
        <Sheet open={open} onOpenChange={onOpenChange}>
          <SheetContent side="bottom" className="max-h-[85vh] px-4 pb-6">
            <SheetHeader className="px-0">
              <SheetTitle>{TITLE}</SheetTitle>
              <SheetDescription>{DESCRIPTION}</SheetDescription>
            </SheetHeader>
            <ToolsBody agentId={agentId} load={load} listingRefused={listingRefused} />
          </SheetContent>
        </Sheet>
      </>
    );
  }

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => onOpenChange(true)}
              disabled={disabled}
              aria-label={TOOLS_LABEL}
              className="text-muted-foreground hover:text-foreground"
              data-testid="tools-toggle"
            >
              <Wrench className="w-4 h-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">
            <p>{TOOLS_LABEL}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[85vh] overflow-hidden flex flex-col">
          <DialogHeader>
            <DialogTitle>{TITLE}</DialogTitle>
            <DialogDescription>{DESCRIPTION}</DialogDescription>
          </DialogHeader>
          <ToolsBody agentId={agentId} load={load} listingRefused={listingRefused} />
        </DialogContent>
      </Dialog>
    </>
  );
}
