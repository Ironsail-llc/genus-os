"use client";

import { LiveCanvas } from "./canvas/live-canvas";

export function VisualPanel() {
  return (
    <div className="h-full w-full" data-testid="visual-panel">
      <LiveCanvas />
    </div>
  );
}
