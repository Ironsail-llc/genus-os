import { RoomRequestError } from "@/lib/rooms/api";
import { ROOM_LABEL } from "@/lib/rooms/labels";
import { roomLink } from "@/lib/rooms/room-id";

/** Opens a room in the shell the way a room link does, without a page load. */
export function goToRoom(roomId: string) {
  window.history.pushState(null, "", roomLink(roomId));
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** A refusal in words: the cap, the server's own message, or its status. */
export function refusalText(err: unknown, doing: string): string {
  if (err instanceof RoomRequestError) {
    if (err.isCap) {
      return err.limit
        ? `Today's limit of ${err.limit} on-demand runs for this ${ROOM_LABEL.singular} is reached. Try again tomorrow.`
        : `Today's limit of on-demand runs for this ${ROOM_LABEL.singular} is reached. Try again tomorrow.`;
    }
    if (err.serverMessage) return err.serverMessage;
    return err.status ? `Could not ${doing}: the server answered ${err.status}.` : `Could not ${doing}: no answer from the server.`;
  }
  return `Could not ${doing}.`;
}
