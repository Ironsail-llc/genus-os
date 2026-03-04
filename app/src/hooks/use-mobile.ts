"use client";

import { useState, useEffect } from "react";

export type ScreenSize = "mobile" | "tablet" | "desktop";

const MOBILE_BREAKPOINT = 768;
const DESKTOP_BREAKPOINT = 1024;

function getScreenSize(): ScreenSize {
  if (typeof window === "undefined") return "desktop";
  const w = window.innerWidth;
  if (w < MOBILE_BREAKPOINT) return "mobile";
  if (w < DESKTOP_BREAKPOINT) return "tablet";
  return "desktop";
}

export function useScreenSize(): ScreenSize {
  const [size, setSize] = useState<ScreenSize>(() => getScreenSize());

  useEffect(() => {
    const mobileQuery = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`);
    const desktopQuery = window.matchMedia(`(min-width: ${DESKTOP_BREAKPOINT}px)`);

    const update = () => setSize(getScreenSize());

    mobileQuery.addEventListener("change", update);
    desktopQuery.addEventListener("change", update);
    return () => {
      mobileQuery.removeEventListener("change", update);
      desktopQuery.removeEventListener("change", update);
    };
  }, []);

  return size;
}

export function useIsMobile(): boolean {
  return useScreenSize() === "mobile";
}
