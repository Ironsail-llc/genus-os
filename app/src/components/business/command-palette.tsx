"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LayoutDashboard, ListTodo, Bot, Store, ShieldAlert, Users, Activity, Workflow, HeartPulse, Sparkles, Moon, Search } from "lucide-react";
import { useTheme } from "next-themes";
import type { ViewId } from "@/components/layout/sidebar";

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
export function CommandPalette({ onNavigate }: { onNavigate: (view: ViewId) => void }) {
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

  const commands = useMemo<Command[]>(() => {
    const nav: Array<{ id: ViewId; label: string; icon: React.ReactNode }> = [
      { id: "dashboard", label: "Go to Dashboard", icon: <LayoutDashboard className="size-4" /> },
      { id: "tasks", label: "Go to Tasks", icon: <ListTodo className="size-4" /> },
      { id: "agents", label: "Go to Agents", icon: <Bot className="size-4" /> },
      { id: "marketplace", label: "Go to Marketplace", icon: <Store className="size-4" /> },
      { id: "controls", label: "Go to Controls", icon: <ShieldAlert className="size-4" /> },
      { id: "fleet", label: "Go to Fleet", icon: <Users className="size-4" /> },
      { id: "runs", label: "Go to Runs", icon: <Activity className="size-4" /> },
      { id: "workflows", label: "Go to Workflows", icon: <Workflow className="size-4" /> },
      { id: "health", label: "Go to Health", icon: <HeartPulse className="size-4" /> },
      { id: "canvas", label: "Go to Canvas", icon: <Sparkles className="size-4" /> },
    ];
    return [
      ...nav.map((n) => ({
        id: n.id as string,
        label: n.label,
        hint: "Navigate",
        icon: n.icon,
        run: () => onNavigate(n.id),
      })),
      {
        id: "toggle-theme",
        label: resolvedTheme === "light" ? "Switch to dark theme" : "Switch to light theme",
        hint: "Appearance",
        icon: <Moon className="size-4" />,
        run: () => setTheme(resolvedTheme === "light" ? "dark" : "light"),
      },
    ];
  }, [onNavigate, resolvedTheme, setTheme]);

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
