"use client";

import { memo, type ComponentProps } from "react";
import { ChatRecovery, type ChatRecoveryRequest } from "@/components/chat-recovery";
import { ChatMarkdown, CHAT_PROSE } from "@/components/chat/chat-markdown";

/** One message in the chat's scrollback. */
export interface ChatBubbleMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  recovery?: ChatRecoveryRequest;
  /** Where a message sent while the agent was working went (live follow-ups). */
  note?: string;
}

interface MessageBubbleProps {
  msg: ChatBubbleMessage;
  /** Must be referentially stable, or every bubble re-renders with the panel. */
  onRecovered: ComponentProps<typeof ChatRecovery>["onRecovered"];
}

/**
 * A message bubble, memoised on its message object.
 *
 * The panel re-renders on every streaming delta. The panel's message updates
 * keep an unchanged message's object as it was, so a history bubble is skipped
 * entirely while a reply streams in below it, instead of re-parsing its
 * markdown each time.
 *
 * `content-visibility: auto` lets the browser skip layout and paint for bubbles
 * scrolled out of view; `contain-intrinsic-size: auto` keeps the size it last
 * measured, so the scrollbar does not jump as they come and go.
 */
export const MessageBubble = memo(function MessageBubble({ msg, onRecovered }: MessageBubbleProps) {
  return (
    <div
      className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"} [content-visibility:auto] [contain-intrinsic-size:auto_3rem]`}
      data-testid={`message-${msg.role}`}
    >
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          msg.role === "user"
            ? "bg-primary text-primary-foreground"
            : "bg-muted border-l-2 border-l-primary"
        }`}
      >
        {msg.role === "assistant" ? (
          msg.recovery ? (
            <div className={CHAT_PROSE}>
              <ChatRecovery request={msg.recovery} messageId={msg.id} onRecovered={onRecovered} />
            </div>
          ) : (
            <ChatMarkdown content={msg.content} />
          )
        ) : (
          <p>{msg.content}</p>
        )}
        {msg.note && (
          <p className="mt-1 text-[11px] opacity-75" data-testid="message-note">{msg.note}</p>
        )}
      </div>
    </div>
  );
});
