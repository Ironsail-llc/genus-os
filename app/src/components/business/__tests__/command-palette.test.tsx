import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { CommandPalette } from "../command-palette";

describe("CommandPalette", () => {
  it("is hidden until ⌘K is pressed", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    expect(screen.queryByTestId("command-palette")).toBeNull();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.getByTestId("command-palette")).toBeTruthy();
  });

  it("opens with ctrl+k too", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(screen.getByTestId("command-palette")).toBeTruthy();
  });

  it("filters commands as you type", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.change(screen.getByTestId("command-input"), { target: { value: "flee" } });
    expect(screen.getByTestId("command-item-fleet")).toBeTruthy();
    expect(screen.queryByTestId("command-item-tasks")).toBeNull();
  });

  it("navigates and closes when a command is chosen", () => {
    const onNavigate = vi.fn();
    render(<CommandPalette onNavigate={onNavigate} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.click(screen.getByTestId("command-item-runs"));
    expect(onNavigate).toHaveBeenCalledWith("runs");
    expect(screen.queryByTestId("command-palette")).toBeNull();
  });

  it("runs Enter on the first visible command", () => {
    const onNavigate = vi.fn();
    render(<CommandPalette onNavigate={onNavigate} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByTestId("command-input");
    fireEvent.change(input, { target: { value: "health" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledWith("health");
  });
});
