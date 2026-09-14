"use client";

import { Construction } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { viewTitles, type ViewId } from "@/components/layout/nav-config";

/** What each not-yet-built view will hold, so the placeholder says something. */
const PROMISES: Partial<Record<ViewId, string>> = {
  inbox: "One queue for everything that wants the operator: agent questions, approvals, mail and alerts.",
  memory: "What the fleet remembers — facts, blocks and entities — with the provenance of every row.",
  audit: "The append-only record of who did what, from which channel, and what the platform decided.",
  logs: "Live engine and service logs, filtered by agent, run and severity.",
};

interface ComingSoonViewProps {
  view: ViewId;
  visible?: boolean;
}

export function ComingSoonView({ view, visible = true }: ComingSoonViewProps) {
  if (!visible) return null;

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4" data-testid={`coming-soon-${view}`}>
      <PageHeader title={viewTitles[view]} />
      <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
        <Construction aria-hidden className="mb-3 size-6 text-muted-foreground/60" strokeWidth={1.5} />
        <p className="text-sm font-medium text-foreground">{viewTitles[view]} is coming soon</p>
        <p className="mt-1 max-w-md text-xs text-muted-foreground">
          {PROMISES[view] ?? "This screen is on the roadmap and is not built yet."}
        </p>
      </div>
    </div>
  );
}
