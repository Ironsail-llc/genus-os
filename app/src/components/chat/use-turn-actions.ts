"use client";

import type { Dispatch, SetStateAction } from "react";
import { useStableCallback } from "@/components/chat/use-stable-callback";

interface TurnActionsOptions<M> {
  messages: M[];
  setMessages: Dispatch<SetStateAction<M[]>>;
  /** A turn is running: both actions do nothing. */
  busy: boolean;
  /** Send `text` as a new turn without touching the composer. */
  resend: (text: string) => void;
  /** Put `text` in the composer and focus it. */
  compose: (text: string) => void;
}

/**
 * Regenerate and Edit-and-resend for the chat's message bubbles, as stable
 * callbacks taking a message id (so memoised bubbles are not re-rendered).
 *
 * Regenerate sends the user turn before the reply again, as a new turn.
 * Edit puts a user message back in the composer and takes it and everything
 * after it off the screen. Local only: the engine keeps its history (it has
 * no truncation), so the agent still sees the earlier exchange.
 */
export function useTurnActions<M extends { id: string; role: string; content: string }>({
  messages,
  setMessages,
  busy,
  resend,
  compose,
}: TurnActionsOptions<M>) {
  const regenerate = useStableCallback((id: string) => {
    if (busy) return;
    const at = messages.findIndex((m) => m.id === id);
    const asked = messages.slice(0, at).reverse().find((m) => m.role === "user");
    if (asked) resend(asked.content);
  });

  const edit = useStableCallback((id: string) => {
    if (busy) return;
    const at = messages.findIndex((m) => m.id === id);
    if (at < 0) return;
    compose(messages[at].content);
    setMessages((prev) => prev.slice(0, at));
  });

  return { regenerate, edit };
}
