/**
 * useTasks through the shared data layer: the shell's live reader and the
 * home metrics' plain reader share one list_tasks request, an optimistic write
 * shows in every reader at once, and an abandoned request is aborted.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/event-bus/use-event-stream", () => ({
  useEventStream: () => ({ events: [], connected: false, synced: false }),
}));

import { useTasks } from "../use-tasks";

function Reader({ id, live }: { id: string; live?: boolean }) {
  const { tasks, approveTask } = useTasks({ live });
  return (
    <>
      <span data-testid={id}>{tasks.map((t) => `${t.id}:${t.status}`).join(",")}</span>
      <button data-testid={`${id}-approve`} onClick={() => void approveTask("1", "looks good")} />
    </>
  );
}

function withClient(children: ReactNode) {
  const client = new QueryClient();
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const signals: AbortSignal[] = [];

function mockTasks(pending = false) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal);
    const body = JSON.parse(String(init?.body ?? "{}")) as { tool?: string };
    if (body.tool === "list_tasks" && pending) return new Promise<Response>(() => {});
    if (body.tool === "list_tasks") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ data: { tasks: [{ id: "1", title: "One", status: "REVIEW" }] } }),
      } as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function listCalls(fetchMock: ReturnType<typeof mockTasks>) {
  return fetchMock.mock.calls.filter(
    ([, init]) => JSON.parse(String(init?.body ?? "{}")).tool === "list_tasks"
  ).length;
}

afterEach(() => {
  signals.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useTasks — shared data layer", () => {
  it("sends one list_tasks request for a live and a plain reader under one client", async () => {
    const fetchMock = mockTasks();
    render(withClient(<><Reader id="shell" live /><Reader id="home" /><Reader id="dash" /></>));
    await waitFor(() => expect(screen.getByTestId("dash").textContent).toBe("1:REVIEW"));
    expect(screen.getByTestId("shell").textContent).toBe("1:REVIEW");
    expect(listCalls(fetchMock)).toBe(1);
  });

  it("shows an optimistic approval in every reader at once", async () => {
    mockTasks();
    render(withClient(<><Reader id="shell" live /><Reader id="home" /></>));
    await waitFor(() => expect(screen.getByTestId("home").textContent).toBe("1:REVIEW"));
    fireEvent.click(screen.getByTestId("shell-approve"));
    // The cache notifies its readers on the next tick, all in one batch.
    await waitFor(() => expect(screen.getByTestId("shell").textContent).toBe("1:DONE"));
    expect(screen.getByTestId("home").textContent).toBe("1:DONE");
  });

  it("aborts the request in flight when the last reader unmounts", async () => {
    mockTasks(true);
    const { unmount } = render(withClient(<Reader id="home" />));
    await waitFor(() => expect(signals).toHaveLength(1));
    unmount();
    await waitFor(() => expect(signals[0].aborted).toBe(true));
  });
});
