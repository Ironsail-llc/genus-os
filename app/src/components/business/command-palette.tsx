"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Moon, Search } from "lucide-react";
import { useTheme } from "next-themes";
import {
  visibleNavGroups,
  type SettingsPageId,
  type ViewId,
} from "@/components/layout/nav-config";

type Command = {
  id: string;
  label: string;
  hint: string;
  icon: React.ReactNode;
  run: () => void;
};

/**
 * ⌘K command palette — navigation plus operator verbs. Hand-rolled (fixed
 * overlay + filtered list) instead of cmdk to stay dependency-free and
 * jsdom-testable.
 */
interface CommandPaletteProps {
  onNavigate: (view: ViewId, settingsPage?: SettingsPageId) => void;
  /** Session role — the palette shows exactly what the sidebar shows. UX gate only. */
  role?: string | null;
}

export function CommandPalette({ onNavigate, role }: CommandPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const { resolvedTheme, setTheme } = useTheme();

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((prev) => !prev);
        setQuery("");
      } else if (e.key === "Escape") {
        close();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Entries are derived from the one nav config the sidebar and the mobile
  // sheet read, so the palette can never drift from the navigation.
  const commands = useMemo<Command[]>(() => {
    const nav = visibleNavGroups(role).flatMap((group) =>
      group.items
        .filter((item) => !item.soon)
        .map((item) => ({
          id: item.id,
          label:
            group.label === item.label
              ? `Go to ${item.label}`
              : `Go to ${group.label} \u203a ${item.label}`,
          hint: "Navigate",
          icon: <item.icon className="size-4" />,
          run: () => onNavigate(item.view, item.sub),
        }))
    );
    return [
      ...nav,
      {
        id: "toggle-theme",
        label: resolvedTheme === "light" ? "Switch to dark theme" : "Switch to light theme",
        hint: "Appearance",
        icon: <Moon className="size-4" />,
        run: () => setTheme(resolvedTheme === "light" ? "dark" : "light"),
      },
    ];
  }, [onNavigate, resolvedTheme, setTheme, role]);

  const visible = commands.filter((c) =>
    c.label.toLowerCase().includes(query.trim().toLowerCase())
  );

  const runCommand = (c: Command) => {
    c.run();
    close();
  };

  if (!open) return null;

  return (
    <div
      data-testid="command-palette"
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-[18vh] backdrop-blur-[2px]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        role="dialog"
        aria-label="Command palette"
        className="w-full max-w-md overflow-hidden rounded-xl border border-border bg-popover shadow-2xl"
      >
        <div className="flex items-center gap-2 border-b border-border px-3">
          <Search aria-hidden className="size-4 text-muted-foreground" />
          <input
            ref={inputRef}
            data-testid="command-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && visible.length > 0) {
                e.preventDefault();
                runCommand(visible[0]);
              }
            }}
            placeholder="Type a command or view…"
            className="h-11 w-full bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground/70"
          />
          <kbd className="rounded border border-border bg-muted px-1.5 font-mono text-[10px] text-muted-foreground">esc</kbd>
        </div>
        <ul className="max-h-72 overflow-y-auto p-1.5">
          {visible.length === 0 && (
            <li className="px-2.5 py-6 text-center text-xs text-muted-foreground">No matching commands.</li>
          )}
          {visible.map((c) => (
            <li key={c.id}>
              <button
                data-testid={`command-item-${c.id}`}
                onClick={() => runCommand(c)}
                className="flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm text-foreground transition-colors hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              >
                <span className="text-muted-foreground">{c.icon}</span>
                {c.label}
                <span className="ml-auto text-[10.5px] uppercase tracking-wide text-muted-foreground/60">{c.hint}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
