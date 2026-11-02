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
