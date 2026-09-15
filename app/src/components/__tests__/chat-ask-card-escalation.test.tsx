import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";

import { ChatAskCard } from "../chat-ask-card";

/**
 * The same SSE event, two completely different rows.
 *
 * `approval_required` is emitted both by `ask_user` (a durable
 * `agent_questions` row, answered at `/api/approvals/question/{id}`) and by a
 * tool-permission escalation (an in-RAM request in the ENGINE, answered at
 * `/api/approvals/escalation/{id}` and proxied there by the bridge). The card
 * posted every one of them to the question route, so an escalation answered
 * from the Helm went to a route that had never heard of that id — the operator
 * saw "Answered", and the agent sat there until its timeout denied the tool.
 *
 * So the branch is on `kind`, and each half is pinned to its own route and its
 * own exact body. The escalation body shapes are the bridge's
 * (`_resolve_escalation`): `approved` is required, `remember_session` is what
 * turns "allow once" into "allow for this session".
 *
 * The countdown is the other half. An escalation is not a durable row a later
 * answer still reaches: the coroutine waiting on it denies and moves on, so a
 * card that kept offering buttons after `timeout_seconds` would be collecting
 * an answer nobody is waiting for.
 */

const ESCALATION = {
  id: "esc-1",
  kind: "escalation" as const,
  question: "Agent scheduler is requesting approval.\nTool: exec\nPolicy: exec-allowlist",
  tool: "exec",
  options: ["Approve", "Approve All", "Deny"],
  timeoutSeconds: 60,
};

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse(String((call[1] as RequestInit).body));
}

describe("ChatAskCard — a tool-permission escalation", () => {
  beforeEach(() => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ settled: true }) });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("names the tool the agent is asking about", () => {
    render(<ChatAskCard {...ESCALATION} />);

    expect(screen.getByTestId("escalation-card")).toBeTruthy();
    expect(screen.getByTestId("escalation-tool").textContent).toContain("exec");
    expect(screen.getByTestId("ask-question").textContent).toContain("requesting approval");
  });

  it("offers exactly the three answers the engine accepts", () => {
    render(<ChatAskCard {...ESCALATION} />);

    expect(screen.getByTestId("escalation-allow-once").textContent).toContain("Allow once");
    expect(screen.getByTestId("escalation-allow-session").textContent).toContain(
      "Allow for this session"
    );
    expect(screen.getByTestId("escalation-deny").textContent).toContain("Deny");
    // The free-text answer box belongs to a question, never to an escalation.
    expect(screen.queryByTestId("ask-input")).toBeNull();
  });

  it("posts {approved:true} to the escalation route for Allow once", async () => {
    render(<ChatAskCard {...ESCALATION} />);

    fireEvent.click(screen.getByTestId("escalation-allow-once"));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const call = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(call[0]).toBe("/api/bridge/api/approvals/escalation/esc-1");
    expect(bodyOf(call)).toEqual({ approved: true });
  });

  it("posts {approved:true, remember_session:true} for Allow for this session", async () => {
    render(<ChatAskCard {...ESCALATION} />);

    fireEvent.click(screen.getByTestId("escalation-allow-session"));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const call = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(call[0]).toBe("/api/bridge/api/approvals/escalation/esc-1");
    expect(bodyOf(call)).toEqual({ approved: true, remember_session: true });
  });

  it("posts {approved:false} for Deny", async () => {
    render(<ChatAskCard {...ESCALATION} />);

    fireEvent.click(screen.getByTestId("escalation-deny"));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const call = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(call[0]).toBe("/api/bridge/api/approvals/escalation/esc-1");
    expect(bodyOf(call)).toEqual({ approved: false });
  });

  it("answers once — a second click cannot send a second decision", async () => {
    render(<ChatAskCard {...ESCALATION} />);

    fireEvent.click(screen.getByTestId("escalation-allow-once"));
    await waitFor(() => expect(screen.getByTestId("ask-status")).toBeTruthy());
    fireEvent.click(screen.getByTestId("escalation-deny"));

    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("counts down, and says the agent decided for itself when it runs out", async () => {
    vi.useFakeTimers();
    render(<ChatAskCard {...ESCALATION} timeoutSeconds={3} />);

    expect(screen.getByTestId("escalation-countdown").textContent).toContain("3s");

    await act(async () => {
      vi.advanceTimersByTime(4000);
    });

    expect(screen.getByTestId("ask-status").textContent).toContain("already decided");
    expect(screen.getByTestId("escalation-allow-once")).toHaveProperty("disabled", true);
    expect(screen.queryByTestId("escalation-countdown")).toBeNull();
  });

  it("says the agent already decided when the engine no longer holds the request", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ settled: false, message: "the engine no longer holds that escalation" }),
    });
    render(<ChatAskCard {...ESCALATION} />);

    fireEvent.click(screen.getByTestId("escalation-allow-once"));

    await waitFor(() =>
      expect(screen.getByTestId("ask-status").textContent).toContain("already decided")
    );
    expect(screen.getByTestId("escalation-deny")).toHaveProperty("disabled", true);
  });

  it("leaves the buttons live when the answer never reached the bridge", async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error("offline"));
    render(<ChatAskCard {...ESCALATION} />);

    fireEvent.click(screen.getByTestId("escalation-allow-once"));

    await waitFor(() => expect(screen.getByTestId("ask-status")).toBeTruthy());
    expect(screen.getByTestId("escalation-allow-once")).toHaveProperty("disabled", false);
  });
});

describe("ChatAskCard — an agent's question is untouched", () => {
  beforeEach(() => {
    global.fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ settled: true }) });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("still posts the chosen option to the QUESTION route", async () => {
    render(<ChatAskCard id="q-7" question="Which vendor?" options={["Acme", "Globex"]} />);

    fireEvent.click(screen.getByTestId("ask-option-1"));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const call = (global.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls[0];
    expect(call[0]).toBe("/api/bridge/api/approvals/question/q-7");
    expect(bodyOf(call)).toEqual({ answer: "Globex" });
  });

  it("renders no escalation controls and no countdown", () => {
    render(<ChatAskCard id="q-7" kind="question" question="Which vendor?" timeoutSeconds={60} />);

    expect(screen.getByTestId("ask-card")).toBeTruthy();
    expect(screen.queryByTestId("escalation-card")).toBeNull();
    expect(screen.queryByTestId("escalation-countdown")).toBeNull();
    expect(screen.getByTestId("ask-input")).toBeTruthy();
  });
});
