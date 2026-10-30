"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Typography for rendered markdown. `prose` comes from the typography plugin
 * loaded in `globals.css`; without it Tailwind's preflight strips paragraph
 * margins, list markers and heading sizes. `dark:prose-invert` follows the
 * app's theme instead of assuming a dark background.
 */
export const CHAT_PROSE = "prose prose-sm dark:prose-invert max-w-none break-words";

const components: Components = {
  a: ({ href, title, children }) => (
    <a href={href} title={title} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

export function ChatMarkdown({ content }: { content: string }) {
  return (
    <div className={CHAT_PROSE}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
