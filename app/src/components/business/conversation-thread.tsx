"use client";

import { ScrollArea } from "@/components/ui/scroll-area";
import type { Message } from "@/lib/api/types";

interface ConversationThreadProps {
  messages: Message[];
  title?: string;
}

export function ConversationThread({
  messages,
  title,
}: ConversationThreadProps) {
  return (
    <div className="flex flex-col h-full" data-testid="conversation-thread">
      {title && (
        <div className="p-3 border-b border-border">
          <h3 className="font-medium">{title}</h3>
        </div>
      )}
      <ScrollArea className="flex-1 p-4">
        <div className="space-y-3">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.message_type === "outgoing" ? "justify-end" : "justify-start"}`}
              data-testid="message-bubble"
            >
              <div
                className={`max-w-[80%] rounded-lg p-3 text-sm ${
                  msg.message_type === "outgoing"
                    ? "bg-primary/20 text-foreground"
                    : "bg-muted text-foreground"
                } ${msg.private ? "border-l-2 border-yellow-500" : ""}`}
              >
                {msg.sender && (
                  <p className="text-xs text-muted-foreground mb-1">
                    {msg.sender.name}
                  </p>
                )}
                <p>{msg.content}</p>
                <p className="text-xs text-muted-foreground mt-1">
                  {new Date(msg.created_at).toLocaleString()}
                </p>
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
