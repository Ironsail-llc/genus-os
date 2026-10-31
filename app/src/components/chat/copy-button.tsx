"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

type CopyState = "idle" | "copied" | "failed";

/**
 * Copy text to the clipboard and say whether it worked.
 *
 * The result is announced in a polite live region, so a screen reader hears
 * "Copied" as well as a sighted reader seeing it; it clears after two seconds.
 */
export function useCopy() {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 2000);
  };

  const status = (
    <span role="status" aria-live="polite" className="sr-only">
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : ""}
    </span>
  );
  return { copy, state, status };
}

export const ACTION_BUTTON =
  "inline-flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-40";

/** Lucide-shaped stroke icons, inline so the chat's tests need no icon mocks. */
export function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      width="14"
      height="14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

export const CopyIcon = () => (
  <Icon>
    <rect x="9" y="9" width="13" height="13" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </Icon>
);

export const CheckIcon = () => (
  <Icon>
    <path d="M20 6 9 17l-5-5" />
  </Icon>
);
