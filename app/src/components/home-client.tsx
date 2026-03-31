"use client";

import { AppShell } from "@/components/layout/app-shell";
import { VisualStateProvider } from "@/hooks/use-visual-state";

export default function HomeClient() {
  return (
    <VisualStateProvider>
      <main className="h-dvh w-full overflow-hidden">
        <AppShell />
      </main>
    </VisualStateProvider>
  );
}
