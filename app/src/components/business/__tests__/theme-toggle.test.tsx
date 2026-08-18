import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";

const setTheme = vi.fn();
let resolvedTheme = "dark";
vi.mock("next-themes", () => ({
  useTheme: () => ({ resolvedTheme, setTheme }),
}));

import { ThemeToggle } from "../theme-toggle";

afterEach(() => {
  setTheme.mockClear();
  resolvedTheme = "dark";
});

describe("ThemeToggle", () => {
  it("switches from dark to light", () => {
    render(<ThemeToggle />);
    screen.getByRole("button", { name: /switch to light theme/i }).click();
    expect(setTheme).toHaveBeenCalledWith("light");
  });

  it("switches from light to dark", () => {
    resolvedTheme = "light";
    render(<ThemeToggle />);
    screen.getByRole("button", { name: /switch to dark theme/i }).click();
    expect(setTheme).toHaveBeenCalledWith("dark");
  });
});
