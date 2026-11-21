/**
 * The Inbox, for workspaces:
 * - the caller's own notifications (workspace notes link to the workspace),
 *   read as THIS user (`toAgent` = their account id), marked read one by one;
 * - a non-operator's own pending approvals, which they may answer.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InboxView } from "../inbox-view";
import type { PendingItem } from "@/lib/inbox/pending";
import { REPORT_ID, reply } from "@/lib/rooms/__tests__/fixtures";
import { ME_ID, NOTE_ID, note } from "@/lib/rooms/__tests__/hub-fixtures";

const OWN: PendingItem = {
  kind: "action",
  id: "77777777-7777-4777-8777-777777777777",
  run_id: "",
  agent_id: "",
  action_kind: "email.send",
  envelope: { recipient: "alice@example.com" },
  question: "email.send: Following up",
  detail: "Hi Alice",
  options: [],
  expires_at: null,
  created_at: null,
  room_id: REPORT_ID,
  mine: true,
};

let notes: unknown[];
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  notes = [
    note(),
    note({
      id: "dddddddd-0000-4000-8000-000000000002",
      subject: "Agent finished",
      metadata: {},
      readAt: "2026-11-20T10:00:00Z",
    }),
  ];
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/bridge/api/auth/me") return reply({ id: ME_ID, role: "member" });
    if (url.startsWith("/api/bridge/api/notifications?")) return reply({ notifications: notes });
    if (url === `/api/bridge/api/notifications/${NOTE_ID}/read` && init?.method === "POST") {
      return reply({ success: true, id: NOTE_ID });
    }
    return reply({ error: "unexpected" }, 500);
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => vi.unstubAllGlobals());

function renderView(overrides: Partial<React.ComponentProps<typeof InboxView>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props: React.ComponentProps<typeof InboxView> = {
    visible: true,
    items: [],
    isLoading: false,
    error: null,
    unrenderable: 0,
    refusedAsNonOperator: false,
    onRefresh: vi.fn(),
    onAnswer: vi.fn().mockResolvedValue({ settled: true, message: null }),
    onOpenRuns: vi.fn(),
    role: "member",
    roleLoading: false,
    ...overrides,
  };
  return render(
    <QueryClientProvider client={client}>
      <InboxView {...props} />
    </QueryClientProvider>,
  );
}

describe("Inbox — notifications", () => {
  it("lists the caller's own notifications, read as this user", async () => {
    renderView();
    const list = await screen.findByTestId("inbox-notifications");
    expect(within(list).getByText("New edition: Week 47")).toBeInTheDocument();
    expect(within(list).getByText("Agent finished")).toBeInTheDocument();
    const listing = fetchMock.mock.calls.map(([u]) => String(u)).find((u) => u.includes("/api/notifications?"))!;
    expect(new URL(listing, "http://x").searchParams.get("toAgent")).toBe(ME_ID);
  });

  it("links a workspace note to its workspace, opened in place", async () => {
    const push = vi.spyOn(window.history, "pushState");
    renderView();
    const link = await screen.findByTestId(`inbox-notification-open-${NOTE_ID}`);
    expect(link).toHaveAttribute("href", `/?v=chat&room=${REPORT_ID}`);
    fireEvent.click(link);
    expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${REPORT_ID}`);
    push.mockRestore();
  });

  it("marks one notification read, for this user only", async () => {
    renderView();
    fireEvent.click(await screen.findByTestId(`inbox-notification-read-${NOTE_ID}`));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/bridge/api/notifications/${NOTE_ID}/read`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(screen.queryByTestId(`inbox-notification-read-${NOTE_ID}`)).toBeNull());
    expect(screen.getByTestId(`inbox-notification-${NOTE_ID}`)).not.toHaveAttribute("data-unread");
  });

  it("offers no mark-read on a note already read", async () => {
    renderView();
    await screen.findByTestId("inbox-notifications");
    expect(screen.queryByTestId("inbox-notification-read-dddddddd-0000-4000-8000-000000000002")).toBeNull();
  });

  it("says so when there are none", async () => {
    notes = [];
    renderView();
    expect(await screen.findByTestId("inbox-notifications-empty")).toBeInTheDocument();
  });

  it("says the notifications could not be read, rather than none", async () => {
    fetchMock.mockImplementation(async () => reply({ error: "down" }, 502));
    renderView();
    expect(await screen.findByTestId("inbox-notifications-error")).toHaveTextContent("502");
  });
});

describe("Inbox — a non-operator's own approvals", () => {
  it("lets them answer what the route listed as theirs", () => {
    renderView({ items: [OWN], role: "member" });
    expect(screen.getByTestId(`inbox-approve-${OWN.id}`)).toBeInTheDocument();
    expect(screen.getByTestId("inbox-own")).toHaveTextContent(/waiting on you/i);
    expect(screen.queryByTestId("inbox-readonly")).toBeNull();
  });

  it("still gives a non-operator no controls on rows that are not theirs", () => {
    renderView({ items: [{ ...OWN, mine: undefined }], role: "member" });
    expect(screen.queryByTestId(`inbox-approve-${OWN.id}`)).toBeNull();
    expect(screen.getByTestId("inbox-readonly")).toBeInTheDocument();
  });
});
