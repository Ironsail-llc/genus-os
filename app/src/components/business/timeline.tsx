"use client";

import { ScrollArea } from "@/components/ui/scroll-area";

interface TimelineEvent {
  id: string;
  title: string;
  description?: string;
  timestamp: string;
  type?: string;
}

interface TimelineProps {
  events: TimelineEvent[];
  title?: string;
}

export function Timeline({ events, title }: TimelineProps) {
  return (
    <div data-testid="timeline">
      {title && <h3 className="font-medium mb-3">{title}</h3>}
      <ScrollArea className="h-full">
        <div className="relative pl-6 space-y-4">
          <div className="absolute left-2 top-0 bottom-0 w-px bg-border" />
          {events.map((event) => (
            <div key={event.id} className="relative" data-testid="timeline-event">
              <div className="absolute -left-4 top-1.5 w-2 h-2 rounded-full bg-primary" />
              <div className="glass-panel p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="font-medium text-sm">{event.title}</span>
                  <span className="text-xs text-muted-foreground">
                    {new Date(event.timestamp).toLocaleString()}
                  </span>
                </div>
                {event.description && (
                  <p className="text-sm text-muted-foreground">
                    {event.description}
                  </p>
                )}
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
