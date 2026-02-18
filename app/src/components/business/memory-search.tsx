"use client";

import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { MemorySearchResult } from "@/lib/api/types";

interface MemorySearchProps {
  results: MemorySearchResult[];
  query?: string;
}

export function MemorySearch({ results, query }: MemorySearchProps) {
  return (
    <div data-testid="memory-search">
      {query && (
        <p className="text-sm text-muted-foreground mb-3">
          Results for: <span className="text-foreground font-medium">{query}</span>
        </p>
      )}
      <ScrollArea className="h-full">
        <div className="space-y-2">
          {results.map((result, idx) => (
            <div
              key={idx}
              className="glass-panel p-3"
              data-testid="memory-result"
            >
              <p className="text-sm mb-2">{result.content}</p>
              <div className="flex items-center gap-2">
                <Badge variant="secondary">{result.category}</Badge>
                {result.similarity !== undefined && (
                  <span className="text-xs text-muted-foreground">
                    {(result.similarity * 100).toFixed(0)}% match
                  </span>
                )}
              </div>
            </div>
          ))}
          {results.length === 0 && (
            <p className="text-sm text-muted-foreground">No results found.</p>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
