import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Activity } from "lucide-react";
import { EmptyState } from "../empty-state";

describe("EmptyState", () => {
  it("renders title and description", () => {
    render(
      <EmptyState
        icon={Activity}
        title="No runs yet today"
        description="Runs appear here as agents execute."
      />
    );
    const el = screen.getByTestId("empty-state");
    expect(el.textContent).toContain("No runs yet today");
    expect(el.textContent).toContain("Runs appear here as agents execute.");
  });

  it("renders an action button and fires its handler", () => {
    const onClick = vi.fn();
    render(
      <EmptyState title="No runs" action={{ label: "View schedules", onClick }} />
    );
    screen.getByRole("button", { name: "View schedules" }).click();
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("renders without icon or action", () => {
    render(<EmptyState title="Nothing here" />);
    expect(screen.getByTestId("empty-state").textContent).toContain("Nothing here");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
