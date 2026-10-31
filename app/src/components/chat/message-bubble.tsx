"use client";

import { memo, type ComponentProps } from "react";
import { ChatRecovery, type ChatRecoveryRequest } from "@/components/chat-recovery";
import { ChatMarkdown, CHAT_PROSE } from "@/components/chat/chat-markdown";
import { MessageActions } from "@/components/chat/message-actions";
import { formatMessageTime, fullMessageTime } from "@/lib/chat/format-timestamp";

/** One message in the chat's scrollback. */
export interface ChatBubbleMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  recovery?: ChatRecoveryRequest;
  /** Where a message sent while the agent was working went (live follow-ups). */
  note?: string;
  /** When it was sent: the stored time for history, absent when unknown. */
  timestamp?: Date;
}

interface MessageBubbleProps {
  msg: ChatBubbleMessage;
  /** Must be referentially stable, or every bubble re-renders with the panel. */
  onRecovered: ComponentProps<typeof ChatRecovery>["onRecovered"];
  /** A turn is running. Must be a plain boolean: it re-renders every bubble when it flips. */
  busy?: boolean;
  /** Both called with the message's id; must be referentially stable like `onRecovered`. */
  onRegenerate?: (id: string) => void;
  onEdit?: (id: string) => void;
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
export const MessageBubble = memo(function MessageBubble({
  msg,
  onRecovered,
  busy = false,
  onRegenerate,
  onEdit,
}: MessageBubbleProps) {
  const isUser = msg.role === "user";
  // `message-<role>` marks the bubble itself, so its text is the message's
  // text alone; the time and the action bar sit under it in the same row.
  return (
    <div
      className={`flex ${isUser ? "justify-end" : "justify-start"} [content-visibility:auto] [contain-intrinsic-size:auto_3rem]`}
      data-testid="message-row"
    >
      <div className={`group flex max-w-[85%] flex-col ${isUser ? "items-end" : "items-start"}`}>
        <div className="flex max-w-full" data-testid={`message-${msg.role}`}>
          <div
            className={`max-w-full rounded-lg px-3 py-2 text-sm ${
              isUser ? "bg-primary text-primary-foreground" : "bg-muted border-l-2 border-l-primary"
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
        {!msg.recovery && (
          <div
            className={`mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground ${isUser ? "flex-row-reverse" : ""}`}
          >
            {msg.timestamp && (
              <time dateTime={msg.timestamp.toISOString()} title={fullMessageTime(msg.timestamp)}>
                {formatMessageTime(msg.timestamp)}
              </time>
            )}
            {/* Hidden until hover or focus, but never removed: the buttons stay in the tab order. */}
            <div className="opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">
              <MessageActions
                role={msg.role}
                content={msg.content}
                busy={busy}
                onRegenerate={onRegenerate && (() => onRegenerate(msg.id))}
                onEdit={onEdit && (() => onEdit(msg.id))}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
});
