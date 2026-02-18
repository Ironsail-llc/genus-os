"use client";

import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { Conversation } from "@/lib/api/types";

interface ConversationListProps {
  conversations: Conversation[];
  onSelect?: (conversation: Conversation) => void;
}

const statusColors: Record<string, string> = {
  open: "bg-green-500/20 text-green-400",
  pending: "bg-yellow-500/20 text-yellow-400",
  resolved: "bg-muted text-muted-foreground",
  snoozed: "bg-blue-500/20 text-blue-400",
};

export function ConversationList({
  conversations,
  onSelect,
}: ConversationListProps) {
  return (
    <ScrollArea className="h-full" data-testid="conversation-list">
      <div className="space-y-2 p-1">
        {conversations.map((convo) => (
          <div
            key={convo.id}
            className="glass-panel p-3 cursor-pointer hover:bg-accent/50 transition-colors"
            onClick={() => onSelect?.(convo)}
            data-testid="conversation-item"
          >
            <div className="flex items-center justify-between mb-1">
              <span className="font-medium text-sm">
                {convo.contact?.name || `Conversation #${convo.id}`}
              </span>
              <Badge
                className={statusColors[convo.status] || ""}
                variant="secondary"
                data-testid="status-badge"
              >
                {convo.status}
              </Badge>
            </div>
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{convo.messages_count} messages</span>
              {convo.unread_count > 0 && (
                <Badge variant="default" className="text-xs" data-testid="unread-badge">
                  {convo.unread_count} unread
                </Badge>
              )}
            </div>
          </div>
        ))}
      </div>
    </ScrollArea>
  );
}
