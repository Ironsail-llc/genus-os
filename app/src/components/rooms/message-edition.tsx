"use client";

import { useQuery } from "@tanstack/react-query";
import { EditionCard } from "@/components/rooms/edition-card";
import { EmailReceipt } from "@/components/rooms/email-receipt";
import { fetchEdition } from "@/lib/rooms/api";
import { useAppQueryClient } from "@/lib/query/query-client";

/**
 * The edition a room message announced, drawn under it. Fetched by id
 * (`GET /api/rooms/{id}/editions/{edition_id}`), because the stored message
 * has had its render markers stripped: this is what puts the charts back
 * after a reload. An edition is immutable once published, so it is read once.
 */
export function MessageEdition({ roomId, editionId }: { roomId: string; editionId: string }) {
  const client = useAppQueryClient();
  const query = useQuery(
    {
      queryKey: ["rooms", roomId, "edition", editionId],
      queryFn: ({ signal }) => fetchEdition(roomId, editionId, signal),
      staleTime: Infinity,
    },
    client,
  );

  if (query.isError) {
    return (
      <p className="mt-2 text-xs text-muted-foreground" data-testid="message-edition-unavailable">
        This edition could not be loaded.
      </p>
    );
  }
  if (!query.data) {
    return <div className="mt-2 h-24 w-full animate-pulse rounded-md bg-background/60" aria-busy="true" aria-label="Loading edition" />;
  }
  return (
    <div className="mt-2 rounded-lg border border-border bg-background p-3" data-testid="message-edition">
      <EditionCard edition={query.data} compact />
    </div>
  );
}

/**
 * What a room message draws under its bubble: the edition it announced, or an
 * email's receipt (a task workspace's receipt turn). One lazy chunk for both.
 */
export function RoomMessageExtras({
  roomId,
  editionId,
  receipt,
}: {
  roomId: string;
  editionId?: string;
  receipt?: unknown;
}) {
  return (
    <>
      {receipt != null && <EmailReceipt receipt={receipt} />}
      {editionId && (
        <div className="w-full">
          <MessageEdition roomId={roomId} editionId={editionId} />
        </div>
      )}
    </>
  );
}
