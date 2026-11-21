import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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
  it("offers 'Open as workspace' on each task", () => {
    render(
      <TasksView
        visible
        tasks={[
          { id: "bbbbbbbb-0000-4000-8000-000000000002", title: "Send the follow-up emails", status: "TODO" },
          { id: "bbbbbbbb-0000-4000-8000-000000000005", title: "Close the quarter", status: "DONE" },
        ]}
        isLoading={false}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onAnswer={vi.fn()}
      />,
    );
    expect(screen.getByTestId("open-as-workspace-bbbbbbbb-0000-4000-8000-000000000002")).toHaveTextContent(
      "Open as workspace",
    );
    expect(screen.getByTestId("open-as-workspace-bbbbbbbb-0000-4000-8000-000000000005")).toBeInTheDocument();
  });
});
