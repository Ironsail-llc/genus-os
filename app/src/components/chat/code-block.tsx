"use client";

import { isValidElement, memo, useMemo, type ReactNode } from "react";
import { tokenize, type TokenType } from "@/lib/chat/highlight";
import { ACTION_BUTTON, CheckIcon, CopyIcon, useCopy } from "@/components/chat/copy-button";

/** Token colours, light and dark. `tok-*` names the kind for tests and user styles. */
const TOKEN_CLASS: Record<TokenType, string> = {
  keyword: "tok-keyword text-violet-700 dark:text-violet-300",
  string: "tok-string text-emerald-700 dark:text-emerald-300",
  comment: "tok-comment italic text-muted-foreground",
  number: "tok-number text-amber-700 dark:text-amber-300",
  plain: "",
};

/** A fenced code block in a chat reply: language label, copy button, highlighting. */
export const CodeBlock = memo(function CodeBlock({ code, language }: { code: string; language?: string }) {
  const { copy, state, status } = useCopy();
  const tokens = useMemo(() => tokenize(code, language), [code, language]);
  return (
    <div className="not-prose my-2 overflow-hidden rounded-md border border-border bg-background/60">
      <div className="flex items-center justify-between border-b border-border px-2 py-0.5">
        <span data-testid="code-language" className="font-mono text-[11px] text-muted-foreground">
          {language || "code"}
        </span>
        <button type="button" aria-label="Copy code" title="Copy code" className={ACTION_BUTTON} onClick={() => copy(code)}>
          {state === "copied" ? <CheckIcon /> : <CopyIcon />}
          <span>{state === "copied" ? "Copied" : "Copy"}</span>
        </button>
        {status}
      </div>
      <pre className="overflow-x-auto p-3 text-xs leading-relaxed">
        <code className={language ? `language-${language}` : undefined}>
          {tokens.map((t, i) =>
            t.type === "plain" ? t.text : <span key={i} className={TOKEN_CLASS[t.type]}>{t.text}</span>,
          )}
        </code>
      </pre>
    </div>
  );
});

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

/**
 * react-markdown's `pre` renderer. A fence arrives as `<pre><code
 * class="language-x">text\n</code></pre>`; inline code never reaches `pre`,
 * so it stays inline. The fence's closing newline is not part of the code.
 */
export function MarkdownPre({ children }: { children?: ReactNode }) {
  const code = Array.isArray(children) ? children[0] : children;
  const className = isValidElement<{ className?: string }>(code) ? code.props.className ?? "" : "";
  const language = /language-([\w+#-]+)/.exec(className)?.[1];
  return <CodeBlock code={textOf(code).replace(/\n$/, "")} language={language} />;
}
