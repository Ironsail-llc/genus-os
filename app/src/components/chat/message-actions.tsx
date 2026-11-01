"use client";

import { markdownToPlainText } from "@/lib/chat/plain-text";
import { ACTION_BUTTON, CheckIcon, CopyIcon, Icon, useCopy } from "@/components/chat/copy-button";

interface MessageActionsProps {
  role: "user" | "assistant";
  content: string;
  /** A turn is running: Regenerate and Edit would start a second one. */
  busy: boolean;
  /** Assistant messages: send the user turn that asked for this one again. */
  onRegenerate?: () => void;
  /** User messages: put the text back in the composer, drop this and what followed. */
  onEdit?: () => void;
}

/**
 * The per-message action bar. The bubble shows it on hover and whenever focus
 * is inside it; it is never `display: none`, so every button stays in the tab
 * order and a keyboard reader reaches it by tabbing through the transcript.
 */
export function MessageActions({ role, content, busy, onRegenerate, onEdit }: MessageActionsProps) {
  const { copy, state, status } = useCopy();
  return (
    <div role="toolbar" aria-label="Message actions" className="flex items-center gap-0.5">
      <button type="button" aria-label="Copy message" title="Copy (markdown)" className={ACTION_BUTTON} onClick={() => copy(content)}>
        {state === "copied" ? <CheckIcon /> : <CopyIcon />}
      </button>
      <button
        type="button"
        aria-label="Copy as plain text"
        title="Copy as plain text"
        className={ACTION_BUTTON}
        onClick={() => copy(markdownToPlainText(content))}
      >
        <Icon>
          <path d="M4 7V4h16v3M9 20h6M12 4v16" />
        </Icon>
      </button>
      {role === "assistant" && onRegenerate && (
        <button
          type="button"
          aria-label="Regenerate response"
          title="Regenerate"
          className={ACTION_BUTTON}
          disabled={busy}
          onClick={onRegenerate}
        >
          <Icon>
            <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
            <path d="M3 3v5h5" />
          </Icon>
        </button>
      )}
      {role === "user" && onEdit && (
        <button
          type="button"
          aria-label="Edit message"
          title="Edit and resend"
          className={ACTION_BUTTON}
          disabled={busy}
          onClick={onEdit}
        >
          <Icon>
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </Icon>
        </button>
      )}
      {status}
    </div>
  );
}
