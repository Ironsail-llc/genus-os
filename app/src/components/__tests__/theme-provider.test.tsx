/**
 * The dashboard follows the OS light/dark setting when the operator has not
 * chosen a theme; a stored choice (from the theme toggle) still wins.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@/components/theme-provider";

function prefersDark(dark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("prefers-color-scheme: dark") ? dark : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.documentElement.className = "";
  document.documentElement.removeAttribute("style");
});

describe("ThemeProvider", () => {
  it("honours an OS light preference when nothing is stored", async () => {
    prefersDark(false);
    render(<ThemeProvider><div /></ThemeProvider>);
    await waitFor(() => expect(document.documentElement.classList.contains("light")).toBe(true));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("honours an OS dark preference when nothing is stored", async () => {
    prefersDark(true);
    render(<ThemeProvider><div /></ThemeProvider>);
    await waitFor(() => expect(document.documentElement.classList.contains("dark")).toBe(true));
  });

  it("lets a stored choice win over the OS", async () => {
    prefersDark(true);
    window.localStorage.setItem("theme", "light");
    render(<ThemeProvider><div /></ThemeProvider>);
    await waitFor(() => expect(document.documentElement.classList.contains("light")).toBe(true));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});
