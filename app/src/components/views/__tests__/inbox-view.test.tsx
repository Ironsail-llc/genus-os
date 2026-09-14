import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { InboxView } from "../inbox-view";
import type { PendingItem } from "@/lib/inbox/pending";

const QUESTION: PendingItem = {
  kind: "question",
  id: "11111111-1111-4111-8111-111111111111",
  run_id: "abcdef12-3456-4789-8abc-def012345678",
  agent_id: "invoice-chaser",
  question: "Which vendor should I chase first?",
  detail: "",
  options: ["Alice", "Bob"],
  expires_at: "2026-06-15T13:00:00Z",
  created_at: "2026-06-15T11:30:00Z",
};

const APPROVAL: PendingItem = {
  kind: "workflow",
  id: "22222222-2222-4222-8222-222222222222",
  run_id: "99887766-5544-4332-8110-aabbccddeeff",
  agent_id: "",
  question: "Send the quote to Alice?",
  detail: Array.from({ length: 12 }, (_, i) => `Line ${i + 1} of the draft.`).join(" "),
  options: [],
  expires_at: "2026-06-15T14:00:00Z",
  created_at: "2026-06-15T11:45:00Z",
};

function renderView(overrides: Partial<React.ComponentProps<typeof InboxView>> = {}) {
  const props: React.ComponentProps<typeof InboxView> = {
    visible: true,
    items: [QUESTION, APPROVAL],
    isLoading: false,
    error: null,
    refusedAsNonOperator: false,
    onRefresh: vi.fn(),
    onAnswer: vi.fn().mockResolvedValue({ settled: true, message: null }),
    onOpenRuns: vi.fn(),
    role: "owner",
    roleLoading: false,
    ...overrides,
  };
  return { props, ...render(<InboxView {...props} />) };
}

describe("InboxView", () => {
  it("renders both kinds with their own pill and the agent that raised them", () => {
    renderView();
    expect(screen.getByTestId(`inbox-kind-${QUESTION.id}`).textContent).toBe("Question");
    expect(screen.getByTestId(`inbox-kind-${APPROVAL.id}`).textContent).toBe("Approval");
    expect(screen.getByTestId(`inbox-who-${QUESTION.id}`).textContent).toBe("invoice-chaser");
    // A workflow row carries no agent; saying so beats an empty slot.
    expect(screen.getByTestId(`inbox-who-${APPROVAL.id}`).textContent).toBe("workflow");
    expect(screen.getByTestId(`inbox-question-${QUESTION.id}`).textContent).toBe(
      QUESTION.question
    );
  });

  it("colour-codes the two kinds differently", () => {
    renderView();
    const question = screen.getByTestId(`inbox-kind-${QUESTION.id}`).className;
    const approval = screen.getByTestId(`inbox-kind-${APPROVAL.id}`).className;
    expect(question).not.toBe(approval);
  });

  it("keeps the list in the order the route sorted it — soonest deadline first", () => {
    renderView();
    const cards = screen.getAllByTestId(/^inbox-card-/);
    expect(cards.map((c) => c.getAttribute("data-testid"))).toEqual([
      `inbox-card-${QUESTION.id}`,
      `inbox-card-${APPROVAL.id}`,
    ]);
  });

  it("dates every card relatively, with the instant on hover", () => {
    renderView();
    const raised = screen.getByTestId(`inbox-raised-${QUESTION.id}`);
    expect(raised.textContent).toMatch(/ago|just now/);
    expect(raised.getAttribute("title")).toContain("2026");
    expect(screen.getByTestId(`inbox-expires-${QUESTION.id}`).getAttribute("title")).toContain(
      "2026"
    );
  });

  it("links to Runs and shows the short run id beside it", () => {
    const onOpenRuns = vi.fn();
    renderView({ onOpenRuns });
    expect(screen.getByTestId(`inbox-run-id-${QUESTION.id}`).textContent).toBe("abcdef12");
    fireEvent.click(screen.getByTestId(`inbox-run-${QUESTION.id}`));
    expect(onOpenRuns).toHaveBeenCalled();
  });

  it("collapses a long detail behind a more control", () => {
    renderView();
    const detail = screen.getByTestId(`inbox-detail-${APPROVAL.id}`);
    expect(detail.className).toContain("line-clamp-3");
    fireEvent.click(screen.getByTestId(`inbox-detail-toggle-${APPROVAL.id}`));
    expect(screen.getByTestId(`inbox-detail-${APPROVAL.id}`).className).not.toContain(
      "line-clamp-3"
    );
  });

  it("offers no detail control for a card that has no detail", () => {
    renderView({ items: [QUESTION] });
    expect(screen.queryByTestId(`inbox-detail-${QUESTION.id}`)).toBeNull();
    expect(screen.queryByTestId(`inbox-detail-toggle-${QUESTION.id}`)).toBeNull();
  });

  it("posts the option the operator clicked as the answer", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ settled: true, message: null });
    renderView({ onAnswer });
    fireEvent.click(screen.getByTestId(`inbox-option-${QUESTION.id}-1`));
    await waitFor(() =>
      expect(onAnswer).toHaveBeenCalledWith(QUESTION.id, "question", { answer: "Bob" })
    );
  });

  it("posts typed free text as the answer", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ settled: true, message: null });
    renderView({ onAnswer });
    fireEvent.change(screen.getByTestId(`inbox-answer-${QUESTION.id}`), {
      target: { value: "Neither — ask Bob first" },
    });
    fireEvent.click(screen.getByTestId(`inbox-send-${QUESTION.id}`));
    await waitFor(() =>
      expect(onAnswer).toHaveBeenCalledWith(QUESTION.id, "question", {
        answer: "Neither — ask Bob first",
      })
    );
  });

  it("will not send an empty answer", () => {
    const onAnswer = vi.fn();
    renderView({ onAnswer });
    expect(screen.getByTestId(`inbox-send-${QUESTION.id}`)).toBeDisabled();
    fireEvent.click(screen.getByTestId(`inbox-send-${QUESTION.id}`));
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("approves with the note beside the button", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ settled: true, message: null });
    renderView({ onAnswer });
    fireEvent.change(screen.getByTestId(`inbox-note-${APPROVAL.id}`), {
      target: { value: "Figures check out." },
    });
    fireEvent.click(screen.getByTestId(`inbox-approve-${APPROVAL.id}`));
    await waitFor(() =>
      expect(onAnswer).toHaveBeenCalledWith(APPROVAL.id, "workflow", {
        approved: true,
        note: "Figures check out.",
      })
    );
  });

  it("rejects, and a note is optional", async () => {
    const onAnswer = vi.fn().mockResolvedValue({ settled: true, message: null });
    renderView({ onAnswer });
    fireEvent.click(screen.getByTestId(`inbox-reject-${APPROVAL.id}`));
    await waitFor(() =>
      expect(onAnswer).toHaveBeenCalledWith(APPROVAL.id, "workflow", {
        approved: false,
        note: "",
      })
    );
  });

  it("shows the card as in flight while the answer is on the wire", async () => {
    let release: (value: { settled: boolean; message: string | null }) => void = () => {};
    const onAnswer = vi.fn(
      () =>
        new Promise<{ settled: boolean; message: string | null }>((resolve) => {
          release = resolve;
        })
    );
    renderView({ onAnswer });
    fireEvent.click(screen.getByTestId(`inbox-option-${QUESTION.id}-0`));

    expect(await screen.findByTestId(`inbox-pending-${QUESTION.id}`)).toBeInTheDocument();
    expect(screen.getByTestId(`inbox-send-${QUESTION.id}`)).toBeDisabled();

    release({ settled: true, message: null });
    await waitFor(() =>
      expect(screen.queryByTestId(`inbox-pending-${QUESTION.id}`)).toBeNull()
    );
  });

  it("renders the server's own message when it did not settle", async () => {
    const onAnswer = vi.fn().mockResolvedValue({
      settled: false,
      message: "Already answered — nothing changed.",
    });
    renderView({ onAnswer });
    fireEvent.click(screen.getByTestId(`inbox-option-${QUESTION.id}-0`));
    expect((await screen.findByTestId(`inbox-message-${QUESTION.id}`)).textContent).toBe(
      "Already answered — nothing changed."
    );
  });

  it("renders a refusal's detail rather than a generic apology", async () => {
    const onAnswer = vi
      .fn()
      .mockResolvedValue({ settled: false, message: "workflow ids are UUIDs" });
    renderView({ onAnswer });
    fireEvent.click(screen.getByTestId(`inbox-approve-${APPROVAL.id}`));
    expect((await screen.findByTestId(`inbox-message-${APPROVAL.id}`)).textContent).toBe(
      "workflow ids are UUIDs"
    );
  });

  it("never asks a browser dialog to confirm anything", () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    renderView();
    fireEvent.click(screen.getByTestId(`inbox-reject-${APPROVAL.id}`));
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("gives a non-operator the list with no controls and a reason", () => {
    renderView({ role: "viewer" });
    expect(screen.getByTestId(`inbox-card-${QUESTION.id}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`inbox-option-${QUESTION.id}-0`)).toBeNull();
    expect(screen.queryByTestId(`inbox-send-${QUESTION.id}`)).toBeNull();
    expect(screen.queryByTestId(`inbox-approve-${APPROVAL.id}`)).toBeNull();
    expect(screen.getByTestId("inbox-readonly").textContent).toMatch(/operator/i);
  });

  it("decides nothing while the session is still resolving", () => {
    renderView({ role: undefined, roleLoading: true });
    expect(screen.queryByTestId("inbox-readonly")).toBeNull();
    expect(screen.getByTestId(`inbox-send-${QUESTION.id}`)).toBeInTheDocument();
  });

  it("says the listing is the operator's when the bridge refuses a member", () => {
    renderView({ items: [], refusedAsNonOperator: true, role: "viewer" });
    expect(screen.getByTestId("inbox-restricted").textContent).toMatch(/operator/i);
    expect(screen.queryByTestId("inbox-empty")).toBeNull();
  });

  it("says nothing is waiting, and where review tasks still live", () => {
    renderView({ items: [] });
    const empty = screen.getByTestId("inbox-empty");
    expect(empty.textContent).toMatch(/Nothing is waiting on you\./);
    expect(empty.textContent).toMatch(/Tasks/);
  });

  it("shows a loading state before the first answer arrives", () => {
    renderView({ items: [], isLoading: true });
    expect(screen.getByTestId("inbox-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("inbox-empty")).toBeNull();
  });

  it("shows a failed listing with a way to try again", () => {
    const onRefresh = vi.fn();
    renderView({ items: [], error: "the engine is down", onRefresh });
    expect(screen.getByTestId("inbox-error").textContent).toContain("the engine is down");
    fireEvent.click(screen.getByTestId("inbox-retry"));
    expect(onRefresh).toHaveBeenCalled();
  });

  it("refreshes on demand", () => {
    const onRefresh = vi.fn();
    renderView({ onRefresh });
    fireEvent.click(screen.getByTestId("inbox-refresh"));
    expect(onRefresh).toHaveBeenCalled();
  });

  it("is hidden rather than unmounted when another view is on screen", () => {
    renderView({ visible: false });
    expect(screen.getByTestId("inbox-view")).toHaveStyle({ display: "none" });
  });
});
