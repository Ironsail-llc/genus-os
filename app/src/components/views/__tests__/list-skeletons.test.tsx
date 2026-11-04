/**
 * Every list view shows skeleton rows while its first read is in flight, not a
 * bare line of text or nothing at all. Each skeleton is a status region named
 * "Loading ..." so a screen reader hears the same thing a sighted operator sees.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { TasksView } from "../tasks-view";
import { AgentsView } from "../agents-view";
import { InboxView } from "../inbox-view";
import { GoalsView } from "../goals-view";
import { MemoryView } from "../memory-view";
import { AuditView } from "../audit-view";
import { LogsView } from "../logs-view";
import { MarketplaceView } from "../marketplace-view";
import { SalesView } from "../sales-view";

// Every read stays in flight for the whole test.
const pending = () => new Promise<Response>(() => {});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(pending));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function expectSkeleton(testId: string) {
  const region = await screen.findByTestId(testId);
  expect(region).toHaveAttribute("role", "status");
  expect(region).toHaveAccessibleName(/loading/i);
  expect(region.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThanOrEqual(3);
}

const noop = () => {};

describe("list view skeletons", () => {
  it("Tasks", async () => {
    render(
      <TasksView visible tasks={[]} isLoading onApprove={noop} onReject={noop} onAnswer={noop} />,
    );
    await expectSkeleton("tasks-loading");
  });

  it("Agents", async () => {
    render(
      <AgentsView
        visible
        agents={[]}
        summary={{ healthy: 0, degraded: 0, failed: 0, sleeping: 0, total: 0 }}
        isLoading
        role="owner"
      />,
    );
    await expectSkeleton("agents-loading");
  });

  it("Inbox", async () => {
    render(
      <InboxView
        visible
        items={[]}
        isLoading
        error={null}
        unrenderable={0}
        refusedAsNonOperator={false}
        onRefresh={noop}
        onAnswer={vi.fn()}
        onOpenRuns={noop}
        role="owner"
      />,
    );
    await expectSkeleton("inbox-loading");
  });

  it("Goals", async () => {
    render(<GoalsView visible />);
    await expectSkeleton("goals-loading");
  });

  it("Memory", async () => {
    render(<MemoryView role="owner" />);
    await expectSkeleton("memory-loading");
  });

  it("Audit", async () => {
    render(<AuditView role="owner" />);
    await expectSkeleton("audit-events-loading");
  });

  it("Logs", async () => {
    render(<LogsView role="owner" />);
    await expectSkeleton("logs-loading");
  });

  it("Marketplace", async () => {
    render(<MarketplaceView visible />);
    await expectSkeleton("marketplace-loading");
  });

  it("Sales", async () => {
    render(<SalesView visible role="admin" />);
    await expectSkeleton("sales-loading");
  });
});
