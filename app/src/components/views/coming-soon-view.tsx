"use client";

import { Construction } from "lucide-react";
import { PageHeader } from "@/components/business/page-header";
import { viewTitles, type ViewId } from "@/components/layout/nav-config";

/**
 * What each not-yet-built view will hold, so the placeholder says something.
 *
 * Empty: Memory, Audit and Logs were the last three entries and all three are
 * built. The component stays for the next view the nav names before it exists —
 * a disabled item with a pill beats a dead link, and that is what `soon` in
 * `nav-config.ts` is for.
 */
const PROMISES: Partial<Record<ViewId, string>> = {};

interface ComingSoonViewProps {
  view: ViewId;
}

/** Mounted only for the view that is showing — the shell does the switching. */
export function ComingSoonView({ view }: ComingSoonViewProps) {
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
