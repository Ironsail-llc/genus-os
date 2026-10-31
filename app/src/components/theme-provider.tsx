"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";

/**
 * Follows the OS light/dark setting until the operator picks a theme; the
 * pick (stored by next-themes) wins from then on.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider attribute="class" defaultTheme="system" enableSystem>
      {children}
    </NextThemesProvider>
  );
}
