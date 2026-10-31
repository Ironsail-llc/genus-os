/**
 * A crash in one surface (the chat panel, one view) shows a readable notice
 * with Retry and Report in that surface's place; the rest of the shell keeps
 * working. Retry re-mounts the subtree; Report posts once to the existing
 * dashboard error reporter.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { SurfaceBoundary } from "@/components/surface-boundary";

let shouldThrow = true;
let mounts = 0;

function Fragile() {
  useEffect(() => {
    mounts += 1;
  }, []);
  if (shouldThrow) throw new Error("kaboom");
  return <p>alive</p>;
}

const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));

beforeEach(() => {
  shouldThrow = true;
  mounts = 0;
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  // React reports a caught render error on the console; keep the run quiet.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SurfaceBoundary", () => {
  it("renders the fallback in place of a throwing child and leaves siblings alone", () => {
    render(
      <div>
        <SurfaceBoundary name="Tasks">
          <Fragile />
        </SurfaceBoundary>
        <p>sibling</p>
      </div>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/Tasks/);
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /report/i })).toBeInTheDocument();
    expect(screen.getByText("sibling")).toBeInTheDocument();
    expect(screen.queryByText("alive")).toBeNull();
  });

  it("re-mounts the child on Retry", () => {
    render(
      <SurfaceBoundary name="Tasks">
        <Fragile />
      </SurfaceBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    shouldThrow = false;
    const before = mounts;
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(screen.getByText("alive")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mounts).toBe(before + 1);
  });

  it("shows the fallback again when the child still throws after Retry", () => {
    render(
      <SurfaceBoundary name="Chat">
        <Fragile />
      </SurfaceBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(screen.getByRole("alert")).toHaveTextContent(/Chat/);
  });

  it("posts one report to the dashboard error reporter", async () => {
    render(
      <SurfaceBoundary name="Tasks">
        <Fragile />
      </SurfaceBoundary>,
    );
    fireEvent.click(screen.getByRole("button", { name: /report/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/dashboard/error");
    expect(init.method).toBe("POST");
    const body = JSON.parse(String(init.body));
    expect(body.source).toBe("surface/tasks");
    expect(body.message).toBe("kaboom");
    // A second click does not post twice.
    fireEvent.click(screen.getByRole("button", { name: /report/i }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows nothing for a failed surface that is not visible", () => {
    render(
      <SurfaceBoundary name="Tasks" visible={false}>
        <Fragile />
      </SurfaceBoundary>,
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
