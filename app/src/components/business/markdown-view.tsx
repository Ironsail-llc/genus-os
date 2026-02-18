"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface MarkdownViewProps {
  content: string;
  title?: string;
}

export function MarkdownView({ content, title }: MarkdownViewProps) {
  return (
    <Card className="glass-panel" data-testid="markdown-view">
      {title && (
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">{title}</CardTitle>
        </CardHeader>
      )}
      <CardContent>
        <ScrollArea className="h-full">
          <div className="prose prose-invert prose-sm max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {content}
            </ReactMarkdown>
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
