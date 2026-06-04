import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TaskBoard } from "@/components/business/task-board";

const mockTasks = [
  { id: "1", title: "Task A", status: "TODO" as const, priority: "urgent", tags: ["email"] },
  { id: "2", title: "Task B", status: "IN_PROGRESS" as const, assignedToAgent: "email-responder" },
  { id: "3", title: "Task C", status: "REVIEW" as const, priority: "high" },
  { id: "4", title: "Task D", status: "DONE" as const },
  { id: "5", title: "Overdue", status: "TODO" as const, slaDeadlineAt: "2020-01-01T00:00:00Z" },
];

describe("TaskBoard", () => {
  it("renders 4 columns including REVIEW", () => {
    render(<TaskBoard tasks={mockTasks} />);
    expect(screen.getByText("To Do")).toBeDefined();
    expect(screen.getByText("In Progress")).toBeDefined();
    expect(screen.getByText("Review")).toBeDefined();
    expect(screen.getByText("Done")).toBeDefined();
  });

  it("has 4-column grid layout", () => {
    render(<TaskBoard tasks={mockTasks} />);
    const board = screen.getByTestId("task-board");
    expect(board.className).toContain("grid-cols-4");
  });

  it("renders task cards", () => {
    render(<TaskBoard tasks={mockTasks} />);
    const cards = screen.getAllByTestId("task-card");
    expect(cards.length).toBe(5);
  });

  it("renders priority badges for non-normal priorities", () => {
    render(<TaskBoard tasks={mockTasks} />);
    const badges = screen.getAllByTestId("priority-badge");
    // Task A (urgent) and Task C (high) should show badges
    expect(badges.length).toBe(2);
    expect(badges[0].textContent).toBe("urgent");
    expect(badges[1].textContent).toBe("high");
  });

  it("renders tag badges", () => {
    render(<TaskBoard tasks={mockTasks} />);
    expect(screen.getByText("email")).toBeDefined();
  });

  it("renders agent assignment", () => {
    render(<TaskBoard tasks={mockTasks} />);
    expect(screen.getByText("email-responder")).toBeDefined();
  });

  it("applies SLA overdue styling", () => {
    render(<TaskBoard tasks={mockTasks} />);
    // The overdue task should have a red ring
    const overdueCard = screen.getByText("Overdue").closest("[data-testid='task-card']");
    expect(overdueCard?.className).toContain("ring-red-500");
  });

  it("does not apply SLA overdue styling to DONE tasks", () => {
    const doneTasks = [
      { id: "1", title: "Done task", status: "DONE" as const, slaDeadlineAt: "2020-01-01T00:00:00Z" },
    ];
    render(<TaskBoard tasks={doneTasks} />);
    const card = screen.getByText("Done task").closest("[data-testid='task-card']");
    expect(card?.className).not.toContain("ring-red-500");
  });

  it("renders approve/reject buttons on REVIEW tasks", () => {
    render(<TaskBoard tasks={mockTasks} />);
    const reviewActions = screen.getByTestId("review-actions");
    expect(reviewActions).toBeDefined();
    expect(screen.getByTestId("approve-button")).toBeDefined();
    expect(screen.getByTestId("reject-button")).toBeDefined();
  });

  it("does not render approve/reject buttons on non-REVIEW tasks", () => {
    const noReviewTasks = [
      { id: "1", title: "Task A", status: "TODO" as const },
      { id: "2", title: "Task B", status: "IN_PROGRESS" as const },
      { id: "3", title: "Task D", status: "DONE" as const },
    ];
    render(<TaskBoard tasks={noReviewTasks} />);
    expect(screen.queryByTestId("review-actions")).toBeNull();
  });

  it("calls onApprove callback when approve is clicked", async () => {
    const onApprove = vi.fn();
    render(<TaskBoard tasks={mockTasks} onApprove={onApprove} />);
    const approveBtn = screen.getByTestId("approve-button");
    approveBtn.click();
    expect(onApprove).toHaveBeenCalledWith("3", "Approved via Helm");
  });

  it("calls onReject callback when reject is clicked", async () => {
    const onReject = vi.fn();
    render(<TaskBoard tasks={mockTasks} onReject={onReject} />);
    const rejectBtn = screen.getByTestId("reject-button");
    rejectBtn.click();
    expect(onReject).toHaveBeenCalledWith("3", "Rejected via Helm");
  });

  // ── Phase 4 — planner-field surface + structured answer endpoint ──

  it("renders objective when present on non-DONE tasks", () => {
    const t = [
      {
        id: "11",
        title: "Acme Vendor",
        status: "TODO" as const,
        objective: "Confirm widget pricing without scheduling a meeting",
      },
    ];
    render(<TaskBoard tasks={t} />);
    const objLine = screen.getByTestId("task-objective");
    expect(objLine.textContent).toContain("Confirm widget pricing");
  });

  it("renders next_action with the assigned agent badge", () => {
    const t = [
      {
        id: "12",
        title: "Acme Vendor",
        status: "IN_PROGRESS" as const,
        nextAction: "Email Bob for written quote",
        nextActionAgent: "email-responder",
      },
    ];
    render(<TaskBoard tasks={t} />);
    const nextLine = screen.getByTestId("task-next-action");
    expect(nextLine.textContent).toContain("Email Bob");
    expect(nextLine.textContent).toContain("@email-responder");
  });

  it("shows the question block + answer UI on REVIEW with questionForOperator", () => {
    const t = [
      {
        id: "13",
        title: "Acme Vendor",
        status: "REVIEW" as const,
        questionForOperator: "Drop Acme Vendor outreach? y/n",
      },
    ];
    render(<TaskBoard tasks={t} />);
    expect(screen.getByTestId("question-block")).toBeDefined();
    expect(screen.getByTestId("question-text").textContent).toContain("Drop Acme Vendor");
    expect(screen.getByTestId("answer-input")).toBeDefined();
    expect(screen.getByTestId("answer-button")).toBeDefined();
    // Override is hidden by default when a question is present.
    expect(screen.queryByTestId("review-actions")).toBeNull();
  });

  it("falls back to approve/reject when no question is set on a REVIEW task", () => {
    const t = [
      {
        id: "14",
        title: "Normal review",
        status: "REVIEW" as const,
      },
    ];
    render(<TaskBoard tasks={t} />);
    expect(screen.getByTestId("approve-button")).toBeDefined();
    expect(screen.queryByTestId("question-block")).toBeNull();
  });

  it("calls onAnswer when the operator submits an answer", async () => {
    const onAnswer = vi.fn();
    const t = [
      {
        id: "15",
        title: "Acme Vendor",
        status: "REVIEW" as const,
        questionForOperator: "Drop?",
      },
    ];
    render(<TaskBoard tasks={t} onAnswer={onAnswer} />);
    const input = screen.getByTestId("answer-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "yes, drop them" } });
    const btn = screen.getByTestId("answer-button");
    btn.click();
    expect(onAnswer).toHaveBeenCalledWith("15", "yes, drop them", "IN_PROGRESS");
  });

  it("renders an escalation badge when escalationCount > 0", () => {
    const t = [
      { id: "17", title: "Stalled", status: "REVIEW" as const, escalationCount: 3 },
    ];
    render(<TaskBoard tasks={t} />);
    const badge = screen.getByTestId("escalation-badge");
    expect(badge.textContent).toContain("3");
  });

  it("does not render an escalation badge when escalationCount is 0 or absent", () => {
    const t = [
      { id: "18", title: "Zero", status: "REVIEW" as const, escalationCount: 0 },
      { id: "19", title: "Absent", status: "TODO" as const },
    ];
    render(<TaskBoard tasks={t} />);
    expect(screen.queryByTestId("escalation-badge")).toBeNull();
  });

  it("reveals override approve/reject when the operator toggles it", () => {
    const t = [
      {
        id: "16",
        title: "Q-bearing",
        status: "REVIEW" as const,
        questionForOperator: "Drop?",
      },
    ];
    render(<TaskBoard tasks={t} />);
    expect(screen.queryByTestId("review-actions")).toBeNull();
    fireEvent.click(screen.getByTestId("override-toggle"));
    expect(screen.getByTestId("review-actions")).toBeDefined();
  });
});
