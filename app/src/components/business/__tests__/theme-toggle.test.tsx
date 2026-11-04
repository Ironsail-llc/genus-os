import { act, render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { hydrateRoot } from "react-dom/client";
import { describe, it, expect, vi, afterEach } from "vitest";

const setTheme = vi.fn();
let resolvedTheme: string | undefined = "dark";
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

  it("hydrates without a mismatch on a light page and offers dark", async () => {
    // The server cannot know the theme: it renders with none resolved.
    resolvedTheme = undefined;
    const html = renderToString(<ThemeToggle />);
    const container = document.createElement("div");
    container.innerHTML = html;
    document.body.appendChild(container);

    // The browser resolves the OS preference (light, nothing stored).
    resolvedTheme = "light";
    const errors: string[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      errors.push(args.map(String).join(" "));
    });
    const recoverable: unknown[] = [];
    await act(async () => {
      hydrateRoot(container, <ThemeToggle />, {
        onRecoverableError: (e) => recoverable.push(e),
      });
    });
    spy.mockRestore();

    expect(errors.filter((e) => /hydrat|did(n't| not) match/i.test(e))).toEqual([]);
    expect(recoverable).toEqual([]);
    expect(screen.getByRole("button")).toHaveAccessibleName(/switch to dark theme/i);
    container.remove();
  });
});
