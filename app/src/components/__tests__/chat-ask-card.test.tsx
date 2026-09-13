import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ChatAskCard } from "../chat-ask-card";

/**
 * The card an agent's question renders as. It answers the durable
 * `agent_questions` row through the bridge's existing approvals route, so there
 * is one answer path for the Helm, the CLI and Telegram rather than three.
 *
 * The two things the tests hold it to: the option the person tapped is the
 * answer that is posted (never an index or a paraphrase), and a refusal or an
 * already-answered row is SHOWN rather than swallowed — a card that silently
 * did nothing would leave the agent waiting and the person believing they had
 * replied.
 */

let fetchCalls: { url: string; method?: string; body?: string }[] = [];

function mockFetch(response: { ok: boolean; status?: number; json?: unknown }) {
  fetchCalls = [];
  global.fetch = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    fetchCalls.push({ url, method: init?.method, body: init?.body as string | undefined });
    return {
      ok: response.ok,
      status: response.status ?? (response.ok ? 200 : 403),
      json: async () => response.json ?? {},
    };
  });
}

describe("ChatAskCard", () => {
  beforeEach(() => {
    mockFetch({ ok: true, json: { settled: true } });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders one button per option and posts the tapped option to the bridge approvals path", async () => {
    render(<ChatAskCard id="q-1" question="Which vendor?" options={["Acme", "Globex"]} />);

    expect(screen.getByTestId("ask-card").textContent).toContain("Which vendor?");
    expect(screen.getAllByTestId(/^ask-option-/)).toHaveLength(2);

    fireEvent.click(screen.getByTestId("ask-option-1"));

    await waitFor(() => {
      expect(fetchCalls).toHaveLength(1);
    });
    expect(fetchCalls[0].url).toBe("/api/bridge/api/approvals/question/q-1");
    expect(fetchCalls[0].method).toBe("POST");
    expect(JSON.parse(fetchCalls[0].body!)).toEqual({ answer: "Globex" });
  });

  it("posts free text when no options are given", async () => {
    render(<ChatAskCard id="q-2" question="When should this go out?" options={[]} />);

    expect(screen.queryAllByTestId(/^ask-option-/)).toHaveLength(0);
    fireEvent.change(screen.getByTestId("ask-input"), { target: { value: "Tuesday" } });
    fireEvent.click(screen.getByTestId("ask-submit"));

    await waitFor(() => {
      expect(fetchCalls).toHaveLength(1);
    });
    expect(JSON.parse(fetchCalls[0].body!)).toEqual({ answer: "Tuesday" });
  });

  it("does not post an empty free-text answer", async () => {
    render(<ChatAskCard id="q-3" question="When?" options={[]} />);

    fireEvent.change(screen.getByTestId("ask-input"), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("ask-submit"));

    expect(fetchCalls).toHaveLength(0);
  });

  it("shows the already-answered message and disables itself on settled:false", async () => {
    mockFetch({ ok: true, json: { settled: false, message: "Already answered — nothing changed." } });
    render(<ChatAskCard id="q-4" question="Which vendor?" options={["Acme"]} />);

    fireEvent.click(screen.getByTestId("ask-option-0"));

    await waitFor(() => {
      expect(screen.getByTestId("ask-status").textContent).toContain("Already answered");
    });
    expect((screen.getByTestId("ask-option-0") as HTMLButtonElement).disabled).toBe(true);
  });

  it("says an operator has to answer when the bridge refuses a member", async () => {
    // C9 ships with require_operator unchanged: a member's own question is
    // answerable only by an owner/admin. The card says so instead of looking
    // like a network failure.
    mockFetch({ ok: false, status: 403, json: { detail: "operator role required" } });
    render(<ChatAskCard id="q-5" question="Which vendor?" options={["Acme"]} />);

    fireEvent.click(screen.getByTestId("ask-option-0"));

    await waitFor(() => {
      expect(screen.getByTestId("ask-status").textContent).toContain("an operator has to answer");
    });
  });

  it("reports a failure and stays answerable so the answer is not lost", async () => {
    mockFetch({ ok: false, status: 502, json: {} });
    render(<ChatAskCard id="q-6" question="Which vendor?" options={["Acme"]} />);

    fireEvent.click(screen.getByTestId("ask-option-0"));

    await waitFor(() => {
      expect(screen.getByTestId("ask-status").textContent).toBeTruthy();
    });
    expect((screen.getByTestId("ask-option-0") as HTMLButtonElement).disabled).toBe(false);
  });

  it("confirms the answer that was recorded, not merely that a request was sent", async () => {
    render(<ChatAskCard id="q-7" question="Which vendor?" options={["Acme", "Globex"]} />);

    fireEvent.click(screen.getByTestId("ask-option-0"));

    await waitFor(() => {
      expect(screen.getByTestId("ask-status").textContent).toContain("Acme");
    });
    expect((screen.getByTestId("ask-option-1") as HTMLButtonElement).disabled).toBe(true);
  });
});
