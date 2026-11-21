"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { MarkdownPre } from "@/components/chat/code-block";
import { appRouteOf, isPlainClick, navigateTo } from "@/lib/rooms/navigate";

/**
 * Typography for rendered markdown. `prose` comes from the typography plugin
 * loaded in `globals.css`; without it Tailwind's preflight strips paragraph
 * margins, list markers and heading sizes. `dark:prose-invert` follows the
 * app's theme instead of assuming a dark background.
 */
export const CHAT_PROSE = "prose prose-sm dark:prose-invert max-w-none break-words";

const components: Components = {
  // Same-page anchors (footnote references and back-links) stay in the tab.
  // A link to a dashboard route (a workspace's `/?v=chat&room={id}`, the link
  // every room tool answers with) opens in place, client-side: a plain click
  // moves the view route, a modified one is left to the browser. Every other
  // link opens a new tab.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  a: ({ node, ...props }) => {
    if (props.href?.startsWith("#")) return <a {...props} />;
    const route = appRouteOf(props.href);
    if (route) {
      return (
        <a
          {...props}
          href={route}
          onClick={(event) => {
            if (!isPlainClick(event)) return;
            event.preventDefault();
            navigateTo(route);
          }}
        />
      );
    }
    return <a {...props} target="_blank" rel="noopener noreferrer" />;
  },
  // Fenced code: language label, copy button, highlighting (code-block.tsx).
  pre: ({ children }) => <MarkdownPre>{children}</MarkdownPre>,
};

const remarkPlugins = [remarkGfm];

/**
 * Memoised by `content`: parsing markdown is the expensive part of a chat
 * render, and a bubble whose text has not changed has nothing to re-parse when
 * the panel around it re-renders for a streaming delta.
 */
export const ChatMarkdown = memo(function ChatMarkdown({ content }: { content: string }) {
  return (
    <div className={CHAT_PROSE}>
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
});
