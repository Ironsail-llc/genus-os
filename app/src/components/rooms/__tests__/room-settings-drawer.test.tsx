/**
 * The settings drawer (A3, A5, G3, F2) and its revision history (A7).
 *
 * An assigned user edits title, instructions, schedule, time zone, default
 * period, notifications and pause; a save carries the revision the form was
 * read at, a stale form is told the definition changed and nothing is
 * overwritten, and a refusal shows the server's message on its field. A
 * shared user reads the same drawer and is offered "Duplicate as mine".
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RoomSettingsDrawer } from "@/components/rooms/room-settings-drawer";
import { AGENT_ID, REPORT_ID, reply, room } from "@/lib/rooms/__tests__/fixtures";
import type { Room } from "@/lib/rooms/types";
import { can } from "@/lib/rooms/access";
import { mockBridge, withClient } from "./controls-harness";

let bridge: ReturnType<typeof mockBridge>;

const REVISIONS = [
  {
    rev: 1,
    changedBy: "u-alice",
    changedByName: "Alice",
    changedAt: "2026-11-01T09:00:00Z",
    definition: { title: "Weekly sales", instructions: "Summarise sales.", settings: { schedule_text: "every Monday at 08:00", tz: "UTC" } },
  },
  {
    rev: 2,
    changedBy: "u-alice",
    changedByName: "Alice",
    changedAt: "2026-11-08T09:00:00Z",
    definition: {
      title: "Weekly sales",
      instructions: "Summarise the week's sales.",
      settings: { schedule_text: "every Monday at 08:00", tz: "UTC" },
    },
  },
  {
    rev: 3,
    changedBy: "u-dana",
    changedByName: "Dana",
    changedAt: "2026-11-15T09:00:00Z",
    definition: {
      title: "Weekly sales",
      instructions: "Summarise the week's sales.",
      settings: { schedule_text: "every weekday at 08:00", tz: "UTC" },
    },
  },
];

const REPORT = room({
  settings: { tz: "UTC", default_grain: "week", schedule_text: "every weekday at 08:00", notify_default: true },
});

beforeEach(() => {
  bridge = mockBridge();
  bridge.on("GET", `/rooms/${REPORT_ID}/revisions`, { revisions: REVISIONS });
});
afterEach(() => vi.unstubAllGlobals());

function drawer(r: Room = REPORT, props: Partial<Parameters<typeof RoomSettingsDrawer>[0]> = {}) {
  const onSaved = vi.fn();
  const onDuplicate = vi.fn();
  const utils = withClient(
    <RoomSettingsDrawer
      room={r}
      can={(action) => can(r.access, action)}
      open
      onOpenChange={vi.fn()}
      onSaved={onSaved}
      onDuplicate={onDuplicate}
      {...props}
    />,
  );
  return { onSaved, onDuplicate, ...utils };
}

const field = (label: RegExp) => screen.getByLabelText(label) as HTMLInputElement;

describe("settings drawer: an assigned user", () => {
  it("shows every setting with its current value, and the schedule in words with the next run", () => {
    drawer();
    const panel = screen.getByTestId("room-settings");
    expect(field(/^title/i).value).toBe("Weekly sales");
    expect((screen.getByLabelText(/instructions/i) as HTMLTextAreaElement).value).toBe("Summarise the week's sales.");
    expect(field(/schedule/i).value).toBe("every weekday at 08:00");
    expect(within(panel).getByTestId("settings-next-run")).toHaveTextContent(/next run.*23 Nov/i);
    expect((screen.getByLabelText(/time zone/i) as HTMLSelectElement).value).toBe("UTC");
    expect((screen.getByLabelText(/default period/i) as HTMLSelectElement).value).toBe("week");
    expect(screen.getByLabelText(/notify people/i)).toBeChecked();
    expect(screen.getByTestId("settings-pause")).toHaveTextContent(/pause/i);
  });

  it("saves only what changed, with the revision the form was read at", async () => {
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, room({ rev: 4, title: "Weekly revenue" }));
    const { onSaved } = drawer();
    fireEvent.change(field(/^title/i), { target: { value: "Weekly revenue" } });
    fireEvent.change(field(/schedule/i), { target: { value: "every Monday at 07:00" } });
    fireEvent.change(screen.getByLabelText(/time zone/i), { target: { value: "Europe/London" } });
    fireEvent.change(screen.getByLabelText(/default period/i), { target: { value: "month" } });
    fireEvent.click(screen.getByLabelText(/notify people/i));
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(1));
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)[0].body).toEqual({
      expectedRev: 3,
      changes: {
        title: "Weekly revenue",
        scheduleText: "every Monday at 07:00",
        tz: "Europe/London",
        defaultGrain: "month",
        notifyDefault: false,
      },
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(await screen.findByTestId("settings-saved")).toBeInTheDocument();
  });

  it("has no cost cap or minimum-interval hint (nothing is capped)", () => {
    drawer();
    const panel = screen.getByTestId("room-settings");
    expect(screen.queryByLabelText(/cost/i)).toBeNull();
    expect(panel).not.toHaveTextContent(/at most|minimum|cap\b|limit/i);
  });

  it("pauses and resumes as a saved change (A5)", async () => {
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, room({ rev: 4, paused: true }));
    drawer();
    fireEvent.click(screen.getByTestId("settings-pause"));
    await waitFor(() => expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(1));
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)[0].body).toEqual({ expectedRev: 3, changes: { paused: true } });
  });

  it("offers resume on a paused report and says runs restart from now", () => {
    drawer(room({ ...REPORT, paused: true }));
    expect(screen.getByTestId("settings-pause")).toHaveTextContent(/resume/i);
    expect(screen.getByTestId("settings-next-run")).toHaveTextContent(/paused/i);
  });

  it("refuses an empty title inline and sends nothing", () => {
    drawer();
    fireEvent.change(field(/^title/i), { target: { value: "  " } });
    fireEvent.click(screen.getByTestId("settings-save"));
    expect(screen.getByTestId("field-error-title")).toHaveTextContent(/title/i);
    expect(field(/^title/i)).toHaveAttribute("aria-invalid", "true");
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(0);
  });

  it("shows the server's refusal on the schedule field and keeps the form", async () => {
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, () =>
      reply({ detail: { error: "invalid_schedule", message: "Could not read the schedule 'whenever'", field: "scheduleText" } }, 422),
    );
    const { onSaved } = drawer();
    fireEvent.change(field(/schedule/i), { target: { value: "whenever" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    expect(await screen.findByTestId("field-error-scheduleText")).toHaveTextContent("Could not read the schedule 'whenever'");
    expect(field(/schedule/i).value).toBe("whenever");
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("shows a refusal of the instructions on that field", async () => {
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, () =>
      reply({ detail: { error: "unsafe_instructions", message: "Instructions are too long (8000 characters at most)" } }, 422),
    );
    drawer();
    fireEvent.change(screen.getByLabelText(/instructions/i), { target: { value: "x".repeat(9000) } });
    fireEvent.click(screen.getByTestId("settings-save"));
    expect(await screen.findByTestId("field-error-instructions")).toHaveTextContent(/too long/i);
  });

  it("tells a stale form the definition changed, offers reload, and never overwrites", async () => {
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, () => reply({ detail: { error: "revision_conflict", currentRev: 4 } }, 409));
    const { onSaved } = drawer();
    fireEvent.change(field(/^title/i), { target: { value: "Mine" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    const conflict = await screen.findByTestId("settings-conflict");
    expect(conflict).toHaveTextContent(/changed since you opened/i);
    expect(conflict).toHaveTextContent(/nothing was saved/i);
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(1);
    fireEvent.click(within(conflict).getByRole("button", { name: /reload/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    // The save stays refused until the form is reloaded on the new revision.
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(1);
  });

  it("reloads an untouched form from a newer revision of the room", () => {
    const { rerender } = drawer();
    const newer = room({ ...REPORT, rev: 4, title: "Changed by Dana" });
    rerender(
      <RoomSettingsDrawer room={newer} can={(a) => can("assigned", a)} open onOpenChange={vi.fn()} onSaved={vi.fn()} onDuplicate={vi.fn()} />,
    );
    expect(field(/^title/i).value).toBe("Changed by Dana");
  });

  it("keeps unsaved edits when someone else saves, says so, and saves nothing over them", async () => {
    const { rerender, onSaved } = drawer();
    fireEvent.change(field(/^title/i), { target: { value: "Mine" } });
    const newer = room({ ...REPORT, rev: 4, title: "Changed by Dana" });
    const props = { can: (a: Parameters<typeof can>[1]) => can("assigned", a), open: true, onOpenChange: vi.fn(), onSaved, onDuplicate: vi.fn() };
    rerender(<RoomSettingsDrawer room={newer} {...props} />);
    expect(field(/^title/i).value).toBe("Mine");
    const conflict = screen.getByTestId("settings-conflict");
    expect(conflict).toHaveTextContent(/changed since you opened/i);
    expect(screen.getByTestId("settings-save")).toBeDisabled();
    // Reload reads the room afresh and takes that revision into the form.
    bridge.on("GET", `/rooms/${REPORT_ID}`, newer);
    fireEvent.click(within(conflict).getByRole("button", { name: /reload/i }));
    await waitFor(() => expect(field(/^title/i).value).toBe("Changed by Dana"));
    expect(screen.queryByTestId("settings-conflict")).toBeNull();
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(0);
  });

  it("takes its own saved revision into the form without a conflict", async () => {
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, room({ ...REPORT, rev: 4, title: "Weekly revenue" }));
    const { rerender, onSaved } = drawer();
    fireEvent.change(field(/^title/i), { target: { value: "Weekly revenue" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    rerender(
      <RoomSettingsDrawer room={room({ ...REPORT, rev: 4, title: "Weekly revenue" })} can={(a) => can("assigned", a)} open onOpenChange={vi.fn()} onSaved={onSaved} onDuplicate={vi.fn()} />,
    );
    expect(screen.queryByTestId("settings-conflict")).toBeNull();
    expect(field(/^title/i).value).toBe("Weekly revenue");
  });

  it("keeps its own saved revision when the refetch is stale or never lands, and saves on from it", async () => {
    let next = 4;
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, (body: unknown) => {
      const changes = (body as { changes: { title?: string } }).changes;
      return reply(room({ ...REPORT, rev: next++, title: changes.title ?? "Weekly revenue" }));
    });
    const { rerender, onSaved } = drawer();
    fireEvent.change(field(/^title/i), { target: { value: "Weekly revenue" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    // The refetch is interrupted: the drawer re-renders on the cached, older room.
    const props = { can: (a: Parameters<typeof can>[1]) => can("assigned", a), open: true, onOpenChange: vi.fn(), onSaved, onDuplicate: vi.fn() };
    rerender(<RoomSettingsDrawer room={REPORT} {...props} />);
    expect(field(/^title/i).value).toBe("Weekly revenue");
    expect(screen.queryByTestId("settings-conflict")).toBeNull();
    fireEvent.change(field(/^title/i), { target: { value: "Weekly revenue by region" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await waitFor(() => expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)).toHaveLength(2));
    expect(bridge.calls("PATCH", `/rooms/${REPORT_ID}`)[1].body).toEqual({
      expectedRev: 4,
      changes: { title: "Weekly revenue by region" },
    });
  });

  it("does not blame someone else when the newer revision is this user's own save", async () => {
    let calls = 0;
    bridge.on("PATCH", `/rooms/${REPORT_ID}`, () => {
      calls += 1;
      // The first save is taken but answers without a room; the second is behind it.
      return calls === 1 ? reply({ ok: true }) : reply({ detail: { error: "revision_conflict", currentRev: 4 } }, 409);
    });
    drawer();
    fireEvent.change(field(/^title/i), { target: { value: "One" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    await screen.findByTestId("settings-saved");
    fireEvent.change(field(/^title/i), { target: { value: "Two" } });
    fireEvent.click(screen.getByTestId("settings-save"));
    const conflict = await screen.findByTestId("settings-conflict");
    expect(conflict).toHaveTextContent(/your own/i);
    expect(conflict).not.toHaveTextContent(/someone else/i);
  });

  it("gives an agent workspace only a name and instructions (F2)", () => {
    drawer(room({ id: AGENT_ID, kind: "agent", settings: {} }));
    expect(field(/^title/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/schedule/i)).toBeNull();
    expect(screen.queryByLabelText(/default period/i)).toBeNull();
    expect(screen.queryByTestId("settings-pause")).toBeNull();
  });
});

describe("settings drawer: a shared user (G3)", () => {
  it("is read-only and offers Duplicate as mine", () => {
    const { onDuplicate } = drawer(room({ ...REPORT, access: "shared" }));
    expect(screen.getByTestId("settings-readonly")).toHaveTextContent(/only the people assigned/i);
    expect(field(/^title/i)).toHaveAttribute("readonly");
    expect(screen.getByLabelText(/instructions/i)).toHaveAttribute("readonly");
    expect(screen.getByLabelText(/time zone/i)).toBeDisabled();
    expect(screen.queryByTestId("settings-save")).toBeNull();
    expect(screen.queryByTestId("settings-pause")).toBeNull();
    fireEvent.click(screen.getByTestId("settings-duplicate"));
    expect(onDuplicate).toHaveBeenCalled();
  });
});

describe("revision history (A7)", () => {
  it("lists who, when and what changed, newest first", async () => {
    drawer();
    fireEvent.click(screen.getByTestId("settings-tab-history"));
    expect(screen.getByTestId("settings-tab-history")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("settings-tab-settings")).toHaveAttribute("aria-selected", "false");
    const r3 = await screen.findByTestId("revision-3");
    expect(r3).toHaveTextContent("Dana");
    expect(r3).toHaveTextContent(/15 Nov/);
    expect(r3).toHaveTextContent(/schedule/i);
    expect(r3).toHaveTextContent("every Monday at 08:00");
    expect(r3).toHaveTextContent("every weekday at 08:00");
    expect(screen.getByTestId("revision-2")).toHaveTextContent(/instructions/i);
    expect(screen.getByTestId("revision-1")).toHaveTextContent(/created/i);
    const items = screen.getAllByTestId(/^revision-\d+$/).map((el) => el.dataset.testid);
    expect(items).toEqual(["revision-3", "revision-2", "revision-1"]);
  });

  it("restores an older revision for an assigned user, not the current one", async () => {
    bridge.on("POST", `/rooms/${REPORT_ID}/revisions/1/restore`, room({ rev: 4 }));
    const { onSaved } = drawer();
    fireEvent.click(screen.getByTestId("settings-tab-history"));
    await screen.findByTestId("revision-3");
    expect(screen.queryByTestId("revision-restore-3")).toBeNull();
    fireEvent.click(screen.getByTestId("revision-restore-1"));
    await waitFor(() => expect(bridge.calls("POST", `/rooms/${REPORT_ID}/revisions/1/restore`)).toHaveLength(1));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("lets a shared user read the history but not restore", async () => {
    drawer(room({ ...REPORT, access: "shared" }));
    fireEvent.click(screen.getByTestId("settings-tab-history"));
    await screen.findByTestId("revision-1");
    expect(screen.queryByTestId(/^revision-restore-/)).toBeNull();
  });
});

