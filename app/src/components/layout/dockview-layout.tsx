"use client";

import {
  DockviewReact,
  type DockviewReadyEvent,
  type IDockviewPanelProps,
} from "dockview-react";
import "dockview-core/dist/styles/dockview.css";
import { VisualPanel } from "@/components/visual-panel";
import { ChatPanel } from "@/components/chat-panel";

const components: Record<
  string,
  React.FC<IDockviewPanelProps>
> = {
  visual: () => <VisualPanel />,
  chat: () => <ChatPanel />,
};

function onReady(event: DockviewReadyEvent) {
  // Calculate pixel widths for 65/35 split
  const containerWidth =
    (event.api as unknown as { element?: HTMLElement }).element?.clientWidth ??
    document.querySelector('[data-testid="dockview-container"]')?.clientWidth ??
    1200;
  const visualWidth = Math.round(containerWidth * 0.65);

  const visualPanel = event.api.addPanel({
    id: "visual",
    component: "visual",
    title: "Dashboard",
    initialWidth: visualWidth,
  });

  event.api.addPanel({
    id: "chat",
    component: "chat",
    title: "Robothor",
    position: { referencePanel: visualPanel, direction: "right" },
  });
}

export function DockviewLayout() {
  return (
    <div
      data-testid="dockview-container"
      className="h-full w-full"
    >
      <DockviewReact
        className="dv-react-app"
        components={components}
        onReady={onReady}
      />
    </div>
  );
}
