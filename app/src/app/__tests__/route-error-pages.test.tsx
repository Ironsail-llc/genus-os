/**
 * The route-level surfaces Next renders on its own: a route error (Retry calls
 * Next's reset, Report posts the digest), a root-layout error, the not-found
 * page and the route loading state. And the viewport allows pinch-zoom.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next/font/google", () => ({
  Geist: () => ({ variable: "font-sans" }),
  Geist_Mono: () => ({ variable: "font-mono" }),
}));

import RouteError from "@/app/error";
import GlobalError from "@/app/global-error";
import NotFound from "@/app/not-found";
import Loading from "@/app/loading";
import { viewport } from "@/app/layout";

const fetchMock = vi.fn(async () => new Response(null, { status: 204 }));

beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function routeError(message: string, digest?: string): Error & { digest?: string } {
  const err = new Error(message) as Error & { digest?: string };
  if (digest) err.digest = digest;
  return err;
}

describe("app/error.tsx", () => {
  it("says the page failed and calls reset once on Retry", () => {
    const reset = vi.fn();
    render(<RouteError error={routeError("boom", "abc123")} reset={reset} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/failed/i);
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("reports the error with its digest to /api/dashboard/error, once", async () => {
    render(<RouteError error={routeError("boom", "abc123")} reset={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /report/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/dashboard/error");
    const body = JSON.parse(String(init.body));
    expect(body.source).toBe("route");
    expect(body.message).toBe("boom");
    expect(body.details).toEqual({ digest: "abc123" });
    fireEvent.click(screen.getByRole("button", { name: /report/i }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows the digest so an operator can quote it", () => {
    render(<RouteError error={routeError("boom", "abc123")} reset={vi.fn()} />);
    expect(screen.getByText(/abc123/)).toBeInTheDocument();
  });
});

describe("app/global-error.tsx", () => {
  it("renders its own notice with Retry and Report", async () => {
    const reset = vi.fn();
    render(<GlobalError error={routeError("layout broke", "d1")} reset={reset} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    expect(reset).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /report/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse(String((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body));
    expect(body.source).toBe("global");
  });
});

describe("app/not-found.tsx", () => {
  it("says the page does not exist and links home", () => {
    render(<NotFound />);
    expect(screen.getByRole("heading")).toHaveTextContent(/not found|does not exist/i);
    expect(screen.getByRole("link", { name: /back/i })).toHaveAttribute("href", "/");
  });
});

describe("app/loading.tsx", () => {
  it("is an accessible loading state with skeleton rows", () => {
    const { container } = render(<Loading />);
    expect(screen.getByRole("status")).toHaveAccessibleName(/loading/i);
    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0);
  });
});

describe("viewport", () => {
  it("does not cap the zoom (pinch-zoom stays available)", () => {
    expect(viewport).not.toHaveProperty("maximumScale");
    expect(viewport).not.toHaveProperty("userScalable", false);
  });
});
