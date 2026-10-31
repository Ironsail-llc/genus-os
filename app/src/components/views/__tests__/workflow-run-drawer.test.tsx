/**
 * The run-detail drawer: one workflow run, step by step, with each step's tool
 * name, its recorded input and output as collapsible pretty-printed JSON, and
 * its error. The engine stores a payload over its size cap as a truncation
 * marker; the drawer says so instead of printing the marker as if it were data.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkflowRunDrawer } from "../automations/workflow-run-drawer";
import { FAILED_RUN_DETAIL, FAILED_RUN_ID, WORKFLOW_ID } from "./workflow-run-fixtures";

afterEach(() => vi.restoreAllMocks());

function reply(body: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

function openDrawer(onOpenChange = () => {}) {
  return render(
    <WorkflowRunDrawer
      workflowId={WORKFLOW_ID}
      runId={FAILED_RUN_ID}
      open
      onOpenChange={onOpenChange}
    />
  );
}

describe("WorkflowRunDrawer", () => {
  it("reads the run's detail from the bridge", async () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    await screen.findByTestId("run-step-update_record");
    expect(spy).toHaveBeenCalledWith(
      `/api/bridge/api/workflows/${WORKFLOW_ID}/runs/${FAILED_RUN_ID}`
    );
  });

  it("lists every step with its tool name and status", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    for (const step of FAILED_RUN_DETAIL.steps) {
      const row = await screen.findByTestId(`run-step-${step.step_id}`);
      expect(row.textContent).toContain(step.tool_name);
      expect(within(row).getByTestId("status-badge").textContent).toContain(step.status);
    }
  });

  it("shows the failing step's error, input and output, opened (AC2)", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    const failed = await screen.findByTestId("run-step-update_record");
    expect(within(failed).getByTestId("run-step-error-update_record").textContent).toContain(
      "write refused: the record is read-only"
    );

    const input = within(failed).getByTestId("run-step-input-update_record") as HTMLDetailsElement;
    const output = within(failed).getByTestId(
      "run-step-output-update_record"
    ) as HTMLDetailsElement;
    expect(input.open).toBe(true);
    expect(output.open).toBe(true);
    // Pretty-printed, not one long line.
    expect(input.querySelector("pre")?.textContent).toBe(
      '{\n  "record_id": "rec-42",\n  "state": "closed"\n}'
    );
    expect(output.querySelector("pre")?.textContent).toContain('"error": "write refused');
  });

  it("keeps a step that succeeded collapsed, and its data still there to open", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    const ok = await screen.findByTestId("run-step-read_record");
    const input = within(ok).getByTestId("run-step-input-read_record") as HTMLDetailsElement;
    expect(input.open).toBe(false);
    expect(input.querySelector("pre")?.textContent).toContain('"record_id": "rec-42"');
    expect(within(ok).queryByTestId("run-step-error-read_record")).toBeNull();
  });

  it("says a truncated payload was truncated, with its size and preview", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    const step = await screen.findByTestId("run-step-export_rows");
    const note = within(step).getByTestId("run-step-input-truncated-export_rows");
    expect(note.textContent).toMatch(/truncated/i);
    expect(note.textContent).toContain("70,000 bytes");
    const input = within(step).getByTestId("run-step-input-export_rows");
    expect(input.querySelector("pre")?.textContent).toBe('{"rows": [{"id": 1}, {"id": 2');
    expect(input.textContent).not.toContain("_truncated");
  });

  it("says a step recorded no output rather than printing an empty box", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    const step = await screen.findByTestId("run-step-export_rows");
    expect(within(step).queryByTestId("run-step-output-export_rows")).toBeNull();
    expect(step.textContent).toMatch(/no output recorded/i);
  });

  it("shows the run's own error and step counts in the header", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    openDrawer();
    const drawer = await screen.findByTestId("workflow-run-drawer");
    await within(drawer).findByTestId("run-step-update_record");
    expect(within(drawer).getByTestId("run-drawer-error-message").textContent).toContain(
      "Step 'update_record' failed"
    );
    expect(drawer.textContent).toContain("1 ok, 1 failed, 1 skipped");
  });

  it("has a loading state", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise(() => {}) as never);
    openDrawer();
    expect(screen.getByTestId("run-drawer-loading")).toBeTruthy();
  });

  it("has an empty state for a run that recorded no steps", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply({ ...FAILED_RUN_DETAIL, steps: [] }));
    openDrawer();
    expect(await screen.findByTestId("run-drawer-empty")).toBeTruthy();
  });

  it("shows a plugin job's returned result instead of an empty step list", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({
        ...FAILED_RUN_DETAIL,
        status: "completed",
        trigger_type: "plugin_job",
        error_message: null,
        steps: [],
        result: { swept: 3, skipped: 0 },
      })
    );
    openDrawer();
    const result = await screen.findByTestId("run-drawer-job-result");
    expect(result.querySelector("pre")?.textContent).toBe('{\n  "swept": 3,\n  "skipped": 0\n}');
    expect(screen.queryByTestId("run-drawer-empty")).toBeNull();
  });

  it("says a plugin job's result was truncated when the engine capped it", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({
        ...FAILED_RUN_DETAIL,
        trigger_type: "plugin_job",
        steps: [],
        result: '{"rows": [',
        result_truncated: true,
      })
    );
    openDrawer();
    const result = await screen.findByTestId("run-drawer-job-result");
    expect(result.textContent).toMatch(/truncated/i);
    expect(result.querySelector("pre")?.textContent).toBe('{"rows": [');
  });

  it("says a plugin job that returned nothing returned nothing", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({ ...FAILED_RUN_DETAIL, trigger_type: "plugin_job", steps: [] })
    );
    openDrawer();
    const empty = await screen.findByTestId("run-drawer-empty");
    expect(empty.textContent).toMatch(/no result recorded/i);
    expect(empty.textContent).not.toMatch(/before any step/i);
  });

  it("lists a retried step once per attempt without duplicate React keys", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const attempt = {
      step_id: "fetch",
      step_type: "tool",
      tool_name: "search_records",
      tool_input: { query: "x" },
    };
    vi.spyOn(global, "fetch").mockResolvedValue(
      reply({
        ...FAILED_RUN_DETAIL,
        steps: [
          { ...attempt, status: "failed", started_at: "2026-06-15T11:00:00+00:00" },
          { ...attempt, status: "completed", started_at: "2026-06-15T11:00:02+00:00" },
        ],
      })
    );
    openDrawer();
    await screen.findAllByTestId("run-step-fetch");
    expect(screen.getAllByTestId("run-step-fetch")).toHaveLength(2);
    const keyWarnings = errors.mock.calls.filter((args) =>
      args.some((a) => typeof a === "string" && a.includes("same key"))
    );
    expect(keyWarnings).toEqual([]);
  });

  it("says an operator-only refusal is one", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply({ detail: "forbidden" }, 403));
    openDrawer();
    expect((await screen.findByTestId("run-drawer-error")).textContent).toMatch(
      /Operator access required/i
    );
  });

  it("says a run that is gone is gone", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply({ detail: "run not found" }, 404));
    openDrawer();
    expect((await screen.findByTestId("run-drawer-error")).textContent).toMatch(/not found/i);
  });

  it("does not fetch while closed", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    render(
      <WorkflowRunDrawer
        workflowId={WORKFLOW_ID}
        runId={FAILED_RUN_ID}
        open={false}
        onOpenChange={() => {}}
      />
    );
    expect(spy).not.toHaveBeenCalled();
    expect(screen.queryByTestId("workflow-run-drawer")).toBeNull();
  });

  it("closes from its close control", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(reply(FAILED_RUN_DETAIL));
    const onOpenChange = vi.fn();
    openDrawer(onOpenChange);
    await screen.findByTestId("run-step-update_record");
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
