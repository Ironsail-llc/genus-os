"use client";

import { EditionBlocks } from "@/components/rooms/edition-card";

/**
 * What a workspace reply rendered (`[RENDER:…]` markers and `render` events),
 * drawn under it with the same renderer and allow-list as an edition. A room
 * never draws on the main conversation's canvas.
 */
export function MessageRenders({ blocks }: { blocks: unknown[] }) {
  return (
    <div className="mt-2 w-full rounded-lg border border-border bg-background p-3" data-testid="message-renders">
      <EditionBlocks blocks={blocks} compact />
    </div>
  );
}
