import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { CommandPalette } from "../command-palette";

describe("keyboard shortcuts help", () => {
  it("opens on ? and lists every shortcut the dashboard has", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull();
    fireEvent.keyDown(document.body, { key: "?", shiftKey: true });
    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    const rows = dialog.querySelectorAll("[data-testid=shortcut-row]");
    const text = [...rows].map((r) => r.textContent ?? "");
    expect(rows).toHaveLength(7);
    for (const label of [
      "Open the command palette",
      "Show keyboard shortcuts",
      "Send the message",
      "New line in the message",
      "Toggle plan mode",
      "Toggle deep reasoning",
      "Close a dialog or palette",
    ]) {
      expect(text.some((t) => t.includes(label))).toBe(true);
    }
  });

  it("closes on Escape", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    fireEvent.keyDown(document.body, { key: "?" });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull();
  });

  it("stays shut when ? is typed into a text field", () => {
    render(
      <>
        <textarea data-testid="composer" />
        <input data-testid="field" />
        <CommandPalette onNavigate={vi.fn()} />
      </>,
    );
    fireEvent.keyDown(screen.getByTestId("composer"), { key: "?" });
    fireEvent.keyDown(screen.getByTestId("field"), { key: "?" });
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull();
  });

  it("has a close button with a label", () => {
    render(<CommandPalette onNavigate={vi.fn()} />);
    fireEvent.keyDown(document.body, { key: "?" });
    fireEvent.click(screen.getByRole("button", { name: "Close keyboard shortcuts" }));
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).toBeNull();
  });
});
