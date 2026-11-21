"use client";

import type { ReactNode } from "react";
import { ComponentRenderer } from "@/components/component-renderer";
import { isRenderableBlock } from "@/lib/rooms/edition-blocks";
import { formatPeriod } from "@/lib/rooms/format";
import type { Edition } from "@/lib/rooms/types";

interface EditionCardProps {
  edition: Edition;
  /** Smaller heading and spacing, for an edition drawn inside a chat message. */
  compact?: boolean;
  /** Controls beside the title (the room view's "back to latest", lane 10's period selector). */
  actions?: ReactNode;
}

/**
 * One edition: its title, period and summary, then its blocks, each drawn by
 * the dashboard's own component renderer (`component-renderer.tsx`).
 *
 * A block the edition list does not allow, or one that is not shaped like a
 * block at all, becomes a visible "unsupported block" placeholder; a block
 * that throws while drawing is caught by the renderer's own boundary. Neither
 * takes the rest of the edition down.
 */
export function EditionCard({ edition, compact = false, actions }: EditionCardProps) {
  const blocks: unknown[] = Array.isArray(edition.blocks) ? edition.blocks : [];
  const period = formatPeriod(edition.periodStart, edition.periodEnd);
  return (
    <article
      className={`flex flex-col ${compact ? "gap-3" : "gap-4"}`}
      data-testid="edition-card"
      aria-label={edition.title || "Edition"}
    >
      <header className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <h2
            className={`${compact ? "text-sm" : "text-base"} font-semibold tracking-tight text-foreground`}
            data-testid="edition-title"
          >
            {edition.title || "Untitled edition"}
          </h2>
          {period && (
            <p className="mt-0.5 text-xs text-muted-foreground tabular-nums" data-testid="edition-period">
              {period}
            </p>
          )}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </header>
      {edition.summary && (
        <p className="text-sm leading-relaxed text-foreground/85" data-testid="edition-summary">
          {edition.summary}
        </p>
      )}
      {blocks.length > 0 && <EditionBlocks blocks={blocks} compact={compact} />}
    </article>
  );
}

/**
 * Blocks drawn the way an edition draws them: each allowed component through
 * the dashboard's renderer, anything else as an "unsupported block"
 * placeholder. Also what a workspace reply uses for its `[RENDER:…]` markers.
 */
export function EditionBlocks({ blocks, compact = false }: { blocks: unknown[]; compact?: boolean }) {
  return (
    <div className={`flex flex-col ${compact ? "gap-3" : "gap-4"}`} data-testid="edition-blocks">
      {blocks.map((block, i) =>
        isRenderableBlock(block) ? (
          <ComponentRenderer key={i} toolName={block.component} props={block.props} />
        ) : (
          <UnsupportedBlock key={i} block={block} />
        ),
      )}
    </div>
  );
}

function UnsupportedBlock({ block }: { block: unknown }) {
  const name =
    block && typeof block === "object" && typeof (block as { component?: unknown }).component === "string"
      ? (block as { component: string }).component
      : null;
  return (
    <div
      className="rounded-lg border border-dashed border-border px-3 py-2.5 text-xs text-muted-foreground"
      data-testid="edition-block-unsupported"
    >
      Unsupported block{name ? <> — <code className="font-mono">{name}</code></> : null}. The rest of this edition is unaffected.
    </div>
  );
}
