"use client";

/** Every keyboard shortcut the dashboard has, in the order a newcomer needs them. */
export const SHORTCUTS: ReadonlyArray<{ keys: string[]; label: string }> = [
  { keys: ["Ctrl/⌘", "K"], label: "Open the command palette" },
  { keys: ["?"], label: "Show keyboard shortcuts" },
  { keys: ["Enter"], label: "Send the message" },
  { keys: ["Shift", "Enter"], label: "New line in the message" },
  { keys: ["Ctrl/⌘", "Shift", "P"], label: "Toggle plan mode" },
  { keys: ["Ctrl/⌘", "Shift", "D"], label: "Toggle deep reasoning" },
  { keys: ["Esc"], label: "Close a dialog or palette" },
];

/** True when a key press is someone typing, so "?" must stay a character. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/** The "?" sheet. Opened and closed by the command palette, which owns the keys. */
export function ShortcutsHelp({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-[14vh] backdrop-blur-[2px]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcuts-help-title"
        className="w-full max-w-sm rounded-xl border border-border bg-popover p-4 shadow-2xl"
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 id="shortcuts-help-title" className="text-sm font-semibold text-foreground">
            Keyboard shortcuts
          </h2>
          <button
            type="button"
            autoFocus
            aria-label="Close keyboard shortcuts"
            onClick={onClose}
            className="rounded px-1.5 text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
          >
            ×
          </button>
        </div>
        <dl className="space-y-1.5">
          {SHORTCUTS.map((s) => (
            <div key={s.label} data-testid="shortcut-row" className="flex items-center justify-between gap-4 text-sm">
              <dt className="text-foreground">{s.label}</dt>
              <dd className="flex gap-1">
                {s.keys.map((k) => (
                  <kbd
                    key={k}
                    className="rounded border border-border bg-muted px-1.5 font-mono text-[11px] text-muted-foreground"
                  >
                    {k}
                  </kbd>
                ))}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}
