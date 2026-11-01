"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { refusalSentence } from "@/lib/bridge/hop-sentence";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";

/**
 * The root tenant's decision about one plugin for one child tenant.
 *
 * Rendered only for the root operator at home (`isRootAtHome`): the grant
 * route is `require_root_operator`, so offering it to anybody else would be a
 * control whose only outcome is a 403.
 *
 * `PUT /api/plugins/{name}/tenants/{tenant}` writes the grant and reloads the
 * child's engine. The answer says whether that reload worked, and a grant whose
 * reload failed is saved but not live — the page says exactly that rather than
 * reporting the write as the whole story.
 */

const BRIDGE = "/api/bridge";

export interface ChildTenant {
  id: string;
  displayName: string;
}

type GrantState = "on" | "off" | null;

interface Outcome {
  text: string;
  live: boolean;
}

function outcomeSentence(tenant: string, state: GrantState, body: unknown): Outcome {
  const answer = (body ?? {}) as Record<string, unknown>;
  const decided =
    state === null
      ? `Grant cleared for ${tenant}; it decides for itself again.`
      : `Forced ${state} for ${tenant}.`;
  if (answer.reloaded === true) {
    return { text: `${decided} Its engine reloaded, so this is live.`, live: true };
  }
  const status = typeof answer.reload_status === "number" ? ` (status ${answer.reload_status})` : "";
  return {
    text: `${decided} The grant is saved, but ${tenant}'s engine could not be reloaded${status}, so it applies on that engine's next reload.`,
    live: false,
  };
}

export function PluginTenantGrant({
  plugin,
  childTenants,
}: {
  plugin: string;
  childTenants: ChildTenant[];
}) {
  const [tenant, setTenant] = useState(childTenants[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The listing can arrive before the picked tenant does; fall back to the first.
  const target = childTenants.some((t) => t.id === tenant) ? tenant : (childTenants[0]?.id ?? "");

  async function grant(state: GrantState) {
    if (!target) return;
    setBusy(true);
    setOutcome(null);
    setError(null);
    try {
      const res = await fetch(
        `${BRIDGE}/api/plugins/${encodeURIComponent(plugin)}/tenants/${encodeURIComponent(target)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ state }),
        },
      );
      if (!res.ok) {
        setError(await refusalSentence(res));
        return;
      }
      setOutcome(outcomeSentence(target, state, await res.json()));
    } catch {
      setError(BRIDGE_UNREACHABLE);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      data-testid={`plugin-grant-${plugin}`}
      className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-border bg-muted/40 p-2"
    >
      <p className="text-[11px] font-medium text-foreground">For a child tenant</p>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor={`plugin-grant-tenant-${plugin}`}>
          Child tenant for {plugin}
        </label>
        <select
          id={`plugin-grant-tenant-${plugin}`}
          data-testid={`plugin-grant-tenant-${plugin}`}
          value={target}
          disabled={busy}
          onChange={(event) => setTenant(event.target.value)}
          className="h-8 min-w-0 max-w-full rounded-md border border-border bg-background px-2 text-xs"
        >
          {childTenants.map((child) => (
            <option key={child.id} value={child.id}>
              {child.displayName}
            </option>
          ))}
        </select>
        <Button
          size="sm"
          variant="outline"
          data-testid={`plugin-grant-on-${plugin}`}
          disabled={busy || !target}
          onClick={() => void grant("on")}
        >
          Force on
        </Button>
        <Button
          size="sm"
          variant="outline"
          data-testid={`plugin-grant-off-${plugin}`}
          disabled={busy || !target}
          onClick={() => void grant("off")}
        >
          Force off
        </Button>
        <Button
          size="sm"
          variant="ghost"
          data-testid={`plugin-grant-clear-${plugin}`}
          disabled={busy || !target}
          onClick={() => void grant(null)}
        >
          Let it decide
        </Button>
      </div>
      {outcome ? (
        <p
          data-testid={`plugin-grant-result-${plugin}`}
          className={`break-words text-[11px] ${outcome.live ? "text-muted-foreground" : "text-warning"}`}
        >
          {outcome.text}
        </p>
      ) : null}
      {error ? (
        <p
          data-testid={`plugin-grant-error-${plugin}`}
          className="break-words text-[11px] text-destructive"
        >
          {error}
        </p>
      ) : null}
    </div>
  );
}
