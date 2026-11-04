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
    expect(onNavigate).toHaveBeenCalledWith("runs", undefined);
    expect(screen.queryByTestId("command-palette")).toBeNull();
  });

  it("runs Enter on the first visible command", () => {
    const onNavigate = vi.fn();
    render(<CommandPalette onNavigate={onNavigate} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByTestId("command-input");
    fireEvent.change(input, { target: { value: "health" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledWith("health", undefined);
  });
  it("derives its entries from the nav config, gate included", () => {
    const { unmount } = render(<CommandPalette onNavigate={vi.fn()} role="owner" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    // Memory, Audit and Inbox WERE absent here; all three are built screens now.
    expect(screen.getByTestId("command-item-memory")).toBeTruthy();
    expect(screen.getByTestId("command-item-audit")).toBeTruthy();
    expect(screen.getByTestId("command-item-inbox")).toBeTruthy();
    expect(screen.getByTestId("command-item-workflows").textContent).toContain("Automations");
    unmount();

    // ⌘K is a nav surface like any other, so it obeys the same per-item gate.
    render(<CommandPalette onNavigate={vi.fn()} role="member" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.queryByTestId("command-item-memory")).toBeNull();
    expect(screen.queryByTestId("command-item-audit")).toBeNull();
    expect(screen.queryByTestId("command-item-logs")).toBeNull();
  });

  it("offers settings pages to an operator only", () => {
    const { unmount } = render(<CommandPalette onNavigate={vi.fn()} role="owner" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.getByTestId("command-item-settings-secrets")).toBeTruthy();
    unmount();

    render(<CommandPalette onNavigate={vi.fn()} role="viewer" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.queryByTestId("command-item-settings-secrets")).toBeNull();
  });

  it("navigates to a settings sub-page", () => {
    const onNavigate = vi.fn();
    render(<CommandPalette onNavigate={onNavigate} role="admin" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.click(screen.getByTestId("command-item-settings-flags"));
    expect(onNavigate).toHaveBeenCalledWith("settings", "flags");
  });

  it("moves a highlight with the arrow keys and runs the highlighted command on Enter", () => {
    const onNavigate = vi.fn();
    render(<CommandPalette onNavigate={onNavigate} role="owner" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");

    const input = screen.getByTestId("command-input");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.getAllByRole("option")[2]).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "false");
    expect(input).toHaveAttribute("aria-activedescendant", screen.getAllByRole("option")[2].id);

    fireEvent.keyDown(input, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledTimes(1);
    const viaKeys = onNavigate.mock.calls[0];

    // The same command, clicked: Enter ran the third item, not the first.
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.click(screen.getAllByRole("option")[2]);
    expect(onNavigate.mock.calls[1]).toEqual(viaKeys);
    expect(viaKeys).not.toEqual(["home", undefined]);
  });

  it("wraps from the first item to the last with ArrowUp, and back with ArrowDown", () => {
    render(<CommandPalette onNavigate={vi.fn()} role="owner" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByTestId("command-input");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    const options = screen.getAllByRole("option");
    expect(options[options.length - 1]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
  });

  it("puts the highlight back on the first match when the query changes", () => {
    render(<CommandPalette onNavigate={vi.fn()} role="owner" />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByTestId("command-input");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.change(input, { target: { value: "go to" } });
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
  });

  it("is a listbox the input controls", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const listbox = screen.getByRole("listbox");
    expect(screen.getByTestId("command-input")).toHaveAttribute("aria-controls", listbox.id);
  });

  it("opens the keyboard shortcuts from its own command", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.click(screen.getByTestId("command-item-shortcuts"));
    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
    expect(screen.queryByTestId("command-palette")).toBeNull();
  });
});
