"use client";

import { DockviewLayout } from "@/components/layout/dockview-layout";
import { VisualStateProvider } from "@/hooks/use-visual-state";

export default function Home() {
  return (
    <VisualStateProvider>
      <main className="h-screen w-screen overflow-hidden">
        <DockviewLayout />
      </main>
    </VisualStateProvider>
  );
}
