import { afterEach, describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { TasksView } from "../tasks-view";

describe("TasksView", () => {
  it("lets its content shrink to the width it is given", () => {
    render(
      <TasksView
        visible
        tasks={[]}
        isLoading={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onAnswer={vi.fn()}
      />,
    );
    const view = screen.getByTestId("tasks-view");
    expect(view.className).toContain("min-w-0");
    const inner = view.firstElementChild as HTMLElement;
    expect(inner.className).toContain("min-w-0");
  });
});

describe("TasksView — open as workspace (E6)", () => {
  const TASKS = [
    { id: "bbbbbbbb-0000-4000-8000-000000000002", title: "Send the follow-up emails", status: "TODO" as const },
    { id: "bbbbbbbb-0000-4000-8000-000000000005", title: "Close the quarter", status: "DONE" as const },
  ];
  function view(roomsStatus: number) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify(roomsStatus === 200 ? { rooms: [] } : { detail: "Not Found" }), {
          status: roomsStatus,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    render(
      <TasksView visible tasks={TASKS} isLoading={false} onApprove={vi.fn()} onReject={vi.fn()} onAnswer={vi.fn()} />,
    );
  }
  afterEach(() => vi.unstubAllGlobals());

  it("offers 'Open as workspace' on each task", async () => {
    view(200);
    expect(await screen.findByTestId("open-as-workspace-bbbbbbbb-0000-4000-8000-000000000002")).toHaveTextContent(
      "Open as workspace",
    );
    expect(screen.getByTestId("open-as-workspace-bbbbbbbb-0000-4000-8000-000000000005")).toBeInTheDocument();
  });

  it("offers none where workspaces are not served", async () => {
    view(404);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText("Open as workspace")).toBeNull();
  });
});
