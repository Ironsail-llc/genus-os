import { render, screen, fireEvent, within } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { RunDetailDrawer } from "../run-detail-drawer";

afterEach(() => vi.restoreAllMocks());

const RUN = {
  id: "r1",
  agent_id: "main",
  trigger_type: "cron",
  trigger_detail: "schedule:daily-digest",
  status: "completed",
  started_at: "2099-01-01T09:00:00+00:00",
  completed_at: "2099-01-01T09:00:42+00:00",
  duration_ms: 42000,
  model_used: "example-model",
  models_attempted: ["example-fallback", "example-model"],
  input_tokens: 1200,
  output_tokens: 300,
  cache_creation_tokens: 100,
  cache_read_tokens: 900,
  total_cost_usd: 0.0123,
  delivery_status: "delivered",
  delivery_channel: "telegram",
  delivered_at: "2099-01-01T09:00:43+00:00",
  verified_status: "unverified_claims",
  outcome_assessment: "partial",
  outcome_notes: "Two of three inboxes were read.",
  task_text: "Summarise the overnight inbox for Alice.",
};

const LONG = {
  _truncated: true,
  total_chars: 9000,
  content: "head\n\n[... truncated 5000 chars ...]\n\ntail",
};

const STEPS = [
  { step_number: 0, step_type: "warmup_phase", tool_name: "memory_blocks", duration_ms: 12 },
  { step_number: 0, step_type: "warmup_phase", tool_name: "context_files", duration_ms: 8 },
  {
    step_number: 1,
    step_type: "llm_call",
    model: "example-model",
    input_tokens: 800,
    output_tokens: 120,
    cache_read_tokens: 600,
    duration_ms: 1500,
  },
  {
    step_number: 2,
    step_type: "tool_call",
    tool_name: "read_inbox",
    duration_ms: 230,
    tool_input: { folder: "inbox", limit: 20 },
    tool_output: LONG,
  },
  {
    step_number: 3,
    step_type: "tool_call",
    tool_name: "send_note",
    duration_ms: 40,
    error_message: "permission denied",
    tool_input: { to: "agent@example.com" },
    tool_output: null,
  },
];

function mockDetail(body: unknown, status = 200) {
  return vi.spyOn(global, "fetch").mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response);
}

describe("RunDetailDrawer", () => {
  it("does not fetch until a run is chosen", () => {
    const spy = mockDetail({});
    render(<RunDetailDrawer runId={null} onClose={() => {}} />);
    expect(spy).not.toHaveBeenCalled();
  });

  it("explains the run in its header", async () => {
    const spy = mockDetail({ run: RUN, steps: STEPS, guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const header = await screen.findByTestId("run-detail-header");
    expect(String(spy.mock.calls[0][0])).toMatch(/\/api\/runs\/r1$/);
    const text = header.textContent ?? "";
    expect(text).toMatch(/main/);
    expect(text).toMatch(/cron/);
    expect(text).toMatch(/schedule:daily-digest/);
    expect(text).toMatch(/42 s/);
    expect(text).toMatch(/example-fallback/);
    expect(text).toMatch(/example-model/);
    expect(text).toMatch(/\$0\.0123/);
    expect(screen.getByTestId("run-detail-tokens").textContent).toMatch(/1,200 in/);
    expect(screen.getByTestId("run-detail-tokens").textContent).toMatch(/300 out/);
    expect(screen.getByTestId("run-detail-tokens").textContent).toMatch(/900 cache read/);
    expect(screen.getByTestId("run-detail-tokens").textContent).toMatch(/100 cache write/);
    expect(screen.getByTestId("run-delivered-detail-r1").textContent).toMatch(/delivered · via telegram/);
    expect(screen.getByTestId("run-verified-r1").textContent).toMatch(/unverified claims/);
    expect(screen.getByTestId("run-detail-outcome").textContent).toMatch(/partial/);
    expect(screen.getByTestId("run-detail-outcome").textContent).toMatch(/Two of three inboxes/);
    expect(screen.getByTestId("run-detail-task").textContent).toMatch(/overnight inbox/);
  });

  it("lists each step with kind, tool or model, duration, tokens and status", async () => {
    mockDetail({ run: RUN, steps: STEPS, guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const llm = await screen.findByTestId("run-step-2");
    expect(llm.textContent).toMatch(/llm call/);
    expect(llm.textContent).toMatch(/example-model/);
    expect(llm.textContent).toMatch(/1\.5 s/);
    expect(llm.textContent).toMatch(/920/);
    expect(llm.textContent).toMatch(/ok/);
    const failed = screen.getByTestId("run-step-4");
    expect(failed.textContent).toMatch(/send_note/);
    expect(failed.textContent).toMatch(/error/);
    expect(screen.getByTestId("step-error-4").textContent).toMatch(/permission denied/);
  });

  it("groups warm-up steps under Warm-up", async () => {
    mockDetail({ run: RUN, steps: STEPS, guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const warmup = await screen.findByTestId("run-warmup");
    expect(warmup.textContent).toMatch(/Warm-up/);
    expect(within(warmup).getByTestId("run-step-0").textContent).toMatch(/memory_blocks/);
    expect(within(warmup).getByTestId("run-step-1").textContent).toMatch(/context_files/);
    expect(within(warmup).queryByTestId("run-step-2")).toBeNull();
  });

  it("expands a tool step to its pretty-printed input and output, stating the cap", async () => {
    mockDetail({ run: RUN, steps: STEPS, guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    expect(screen.queryByTestId("step-io-3")).toBeNull();
    fireEvent.click(await screen.findByTestId("step-io-toggle-3"));
    const input = await screen.findByTestId("step-input-3");
    expect(input.textContent).toContain('"folder": "inbox"');
    const output = screen.getByTestId("step-output-3");
    expect(output.textContent).toMatch(/truncated 5000 chars/);
    expect(screen.getByTestId("step-truncated-3").textContent).toMatch(/4,000/);
    expect(screen.getByTestId("step-truncated-3").textContent).toMatch(/9,000/);
  });

  it("does not claim truncation for a short output", async () => {
    mockDetail({ run: RUN, steps: STEPS, guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId("step-io-toggle-4"));
    expect(await screen.findByTestId("step-input-4")).toBeTruthy();
    expect(screen.queryByTestId("step-truncated-4")).toBeNull();
  });

  it("shows guardrail events with their reason", async () => {
    mockDetail({
      run: RUN,
      steps: STEPS,
      guardrail_events: [
        {
          step_number: 3,
          guardrail_name: "exec_allowlist_strict",
          action: "blocked",
          tool_name: "send_note",
          reason: "not on the allowlist",
        },
      ],
    });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const event = await screen.findByTestId("guardrail-event-0");
    expect(event.textContent).toMatch(/not on the allowlist/);
    expect(event.className).toMatch(/warning/);
  });

  it("says steps were pruned when a finished run older than retention has none", async () => {
    // Older than the 30-day step retention: only then is "pruned" the explanation.
    const old = { ...RUN, started_at: new Date(Date.now() - 45 * 864e5).toISOString() };
    mockDetail({ run: old, steps: [], guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const note = await screen.findByTestId("run-steps-pruned");
    expect(note.textContent).toMatch(/30 days/);
    expect(screen.queryByTestId("run-steps-none")).toBeNull();
  });

  it("a recent finished run with no steps says none were recorded, not pruned", async () => {
    const recent = { ...RUN, started_at: new Date(Date.now() - 2 * 864e5).toISOString() };
    mockDetail({ run: recent, steps: [], guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const note = await screen.findByTestId("run-steps-none");
    expect(note.textContent).toMatch(/No steps were recorded for this run/);
    expect(screen.queryByTestId("run-steps-pruned")).toBeNull();
  });

  it("does not say pruned for a run still going", async () => {
    mockDetail({ run: { ...RUN, status: "running" }, steps: [], guardrail_events: [] });
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    await screen.findByTestId("run-detail-header");
    expect(screen.queryByTestId("run-steps-pruned")).toBeNull();
  });

  it("says operator access is required on a 403", async () => {
    mockDetail({ detail: "forbidden" }, 403);
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const error = await screen.findByTestId("run-detail-error");
    expect(error.textContent).toMatch(/operator access is required/i);
  });

  it("says the run is gone on a 404", async () => {
    mockDetail({ detail: "unknown run" }, 404);
    render(<RunDetailDrawer runId="r1" onClose={() => {}} />);
    const error = await screen.findByTestId("run-detail-error");
    expect(error.textContent).toMatch(/run is gone/i);
  });

  it("closes from its close button", async () => {
    mockDetail({ run: RUN, steps: [], guardrail_events: [] });
    const onClose = vi.fn();
    render(<RunDetailDrawer runId="r1" onClose={onClose} />);
    await screen.findByTestId("run-detail-header");
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });
});
