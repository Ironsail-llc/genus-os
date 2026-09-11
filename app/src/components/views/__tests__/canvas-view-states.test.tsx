import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { CanvasView } from "../canvas-view";

/**
 * The Canvas tab must never present a blank pane. With nothing to render it
 * explains what the tab is and offers the action that fills it; when
 * generation fails it shows the failure text instead of an empty iframe.
 *
 * Lives in its own file because the view keeps a module-level
 * stale-while-revalidate cache of the last good HTML — neither spec here ever
 * populates it.
 */

function jsonResponse(body: unknown): Response {
  const text = JSON.stringify(body);
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => JSON.parse(text),
    text: async () => text,
  } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CanvasView with nothing to render", () => {
  it("explains what the tab is and offers the generate action", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
    render(<CanvasView visible />);

    const empty = await screen.findByTestId("canvas-empty");
    expect(empty.textContent).toMatch(/AI-generated view of your data/i);
    expect(screen.getByRole("button", { name: /generate/i })).toBeTruthy();
    expect(screen.queryByTestId("canvas-srcdoc-renderer")).toBeNull();
  });

  it("surfaces the generation error instead of a blank iframe", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: "Dashboard service temporarily unavailable" })),
    );
    render(<CanvasView visible />);

    // Distinct from live-canvas's own `canvas-error`: these are two different
    // panes and a shared id would make either assertion ambiguous.
    const error = await screen.findByTestId("canvas-generation-error");
    expect(error.textContent).toContain("Dashboard service temporarily unavailable");
    expect(screen.getByRole("button", { name: /(generate|retry)/i })).toBeTruthy();
    expect(screen.queryByTestId("canvas-srcdoc-renderer")).toBeNull();
  });
});
