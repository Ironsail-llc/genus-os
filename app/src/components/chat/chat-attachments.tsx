"use client";

/**
 * The chat composer's attachments: the paperclip, the chips for what is about
 * to go, the inline refusals, and the chips a sent bubble keeps.
 *
 * The limits live in `@/lib/chat/attachments` so the composer, the send route
 * and the engine all state the same three numbers. A refusal is a line under
 * the composer, never a browser alert: the operator keeps what they typed and
 * the files that did fit.
 */
import { useCallback, useRef, useState } from "react";
import { FileText, Image as ImageIcon, Paperclip, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  ACCEPT_ATTRIBUTE,
  ATTACHMENT_LIMITS_TEXT,
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_SIZE_TEXT,
  attachmentKind,
  attachmentMime,
  formatSize,
  readAsBase64,
  type AttachmentPayload,
  type AttachmentSummary,
} from "@/lib/chat/attachments";

export interface PendingAttachment extends AttachmentPayload {
  id: string;
}

/** Why files cannot be added right now, said the same way wherever they come from. */
export const NOT_IN_PLAN_OR_DEEP = "Files go with an ordinary message, not in Plan or Deep mode.";
export const NOT_WHILE_RUNNING = "Files go with your next message, not the running task.";

/**
 * What the composer holds, and the one refusal it is showing.
 *
 * `blocked` is the reason files cannot be added at the moment (Plan or Deep
 * mode, a running reply), or null. The paperclip is disabled then, and a
 * paste or a drop gets that reason as an inline refusal, so no route in can
 * leave a file pending that the next send would silently ignore.
 */
export function useChatAttachments(blocked: string | null = null) {
  const [files, setFiles] = useState<PendingAttachment[]>([]);
  const [refusal, setRefusal] = useState<string | null>(null);
  // The count the NEXT add is checked against: a paste and a drop can land
  // before the first read finishes, and state would still say zero.
  const held = useRef<PendingAttachment[]>([]);
  // Bumped by every clear and drop. A read that started before the last one
  // finishes into a composer that has since been emptied (the message was
  // sent, or Plan mode was switched on) and must not put its file back.
  const epoch = useRef(0);

  const commit = (next: PendingAttachment[]) => {
    held.current = next;
    setFiles(next);
  };

  const add = useCallback(async (incoming: ArrayLike<File> | null | undefined) => {
    const list = Array.from(incoming ?? []);
    if (!list.length) return;
    if (blocked) {
      setRefusal(blocked);
      return;
    }
    const refusals: string[] = [];
    const accepted: Array<{ file: File; mime: string }> = [];
    for (const file of list) {
      const mime = attachmentMime(file.name, file.type);
      if (!mime) refusals.push(`${file.name} is not a supported type.`);
      else if (file.size > MAX_ATTACHMENT_BYTES) refusals.push(`${file.name} is larger than ${MAX_ATTACHMENT_SIZE_TEXT}.`);
      else if (held.current.length + accepted.length >= MAX_ATTACHMENTS) {
        refusals.push(`Only ${MAX_ATTACHMENTS} files per message: ${file.name} was not added.`);
      } else accepted.push({ file, mime });
    }
    // Reserve the slots before reading, for the same reason `held` exists.
    const placeholders = accepted.map(({ file, mime }) => ({
      id: crypto.randomUUID(), name: file.name, mime, size: file.size, data: "",
    }));
    held.current = [...held.current, ...placeholders];
    const started = epoch.current;
    const read = await Promise.all(
      accepted.map(async ({ file }, i) => {
        try {
          return { ...placeholders[i], data: await readAsBase64(file) };
        } catch {
          refusals.push(`${file.name} could not be read.`);
          return null;
        }
      })
    );
    if (epoch.current !== started) return;
    const ids = new Set(placeholders.map((p) => p.id));
    const done = read.filter((r): r is PendingAttachment => r !== null);
    commit([...held.current.filter((f) => !ids.has(f.id)), ...done]);
    setRefusal(refusals.length ? refusals.join(" ") : null);
  }, [blocked]);

  const remove = useCallback((id: string) => {
    commit(held.current.filter((f) => f.id !== id));
    setRefusal(null);
  }, []);

  const clear = useCallback(() => {
    epoch.current += 1;
    commit([]);
    setRefusal(null);
  }, []);

  /** Drop everything pending, saying which files went and why. */
  const drop = useCallback((why: string) => {
    if (!held.current.length) return;
    const gone = held.current;
    epoch.current += 1;
    commit([]);
    setRefusal(`${gone.map((f) => f.name).join(", ")} ${gone.length === 1 ? "was" : "were"} removed: ${why}`);
  }, []);

  return { files, refusal, add, remove, clear, drop };
}

/** The paperclip and the hidden input it opens. */
export function AttachButton({ onFiles, disabled }: { onFiles: (files: FileList | null) => void; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => input.current?.click()}
              disabled={disabled}
              aria-label="Attach files"
              className="text-muted-foreground hover:text-foreground shrink-0"
              data-testid="attach-button"
            >
              <Paperclip className="w-4 h-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">
            <p>Attach files: {ATTACHMENT_LIMITS_TEXT}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <input
        ref={input}
        type="file"
        multiple
        accept={ACCEPT_ATTRIBUTE}
        hidden
        onChange={(e) => {
          onFiles(e.target.files);
          e.target.value = ""; // the same file picked twice is two picks
        }}
        data-testid="attach-input"
      />
    </>
  );
}

function KindIcon({ kind }: { kind: string }) {
  return kind === "image" ? <ImageIcon className="w-3.5 h-3.5 shrink-0" /> : <FileText className="w-3.5 h-3.5 shrink-0" />;
}

/** The files about to go with the turn, the limits, and any refusal. */
export function PendingAttachments({
  files, refusal, onRemove,
}: { files: PendingAttachment[]; refusal: string | null; onRemove: (id: string) => void }) {
  if (!files.length && !refusal) return null;
  return (
    <div className="space-y-1" data-testid="pending-attachments">
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {files.map((f) => (
            <span
              key={f.id}
              className="flex items-center gap-1.5 max-w-full rounded-md border border-border bg-muted/50 pl-2 pr-1 py-1 text-xs"
              data-testid="attachment-chip"
            >
              <KindIcon kind={attachmentKind(f.mime)} />
              <span className="truncate max-w-[180px]">{f.name}</span>
              <span className="text-muted-foreground">{formatSize(f.size)}</span>
              <button
                type="button"
                onClick={() => onRemove(f.id)}
                aria-label={`Remove ${f.name}`}
                className="rounded p-0.5 text-muted-foreground hover:text-foreground hover:bg-accent"
              >
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      {refusal && (
        <p role="status" className="text-xs text-destructive" data-testid="attach-refusal">{refusal}</p>
      )}
      <p className="text-[11px] text-muted-foreground" data-testid="attach-limits">{ATTACHMENT_LIMITS_TEXT}</p>
    </div>
  );
}

/** The files a sent (or reloaded) turn carried, inside its bubble. */
export function MessageAttachments({ attachments }: { attachments?: AttachmentSummary[] }) {
  if (!attachments?.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {attachments.map((a, i) => (
        <span
          key={`${a.name}-${i}`}
          className="flex items-center gap-1 rounded border border-primary-foreground/30 px-1.5 py-0.5 text-[11px]"
          data-testid="message-attachment"
        >
          <KindIcon kind={a.kind} />
          <span className="truncate max-w-[160px]">{a.name}</span>
          <span className="opacity-75">{formatSize(a.size)}</span>
        </span>
      ))}
    </div>
  );
}
