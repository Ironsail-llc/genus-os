/**
 * Where an edition's numbers came from (B3): the run, when, who it ran as and
 * the tool calls, from the provenance the server recorded. And this user's
 * own notification choices for one workspace (C7): mute, in-app, and an
 * external channel only when one is linked.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProvenanceDrawer } from "@/components/rooms/provenance-drawer";
import { RoomNotifyDialog } from "@/components/rooms/room-notify-dialog";
import { REPORT_ID, edition, reply, room } from "@/lib/rooms/__tests__/fixtures";
import { mockBridge, withClient } from "./controls-harness";

let bridge: ReturnType<typeof mockBridge>;
beforeEach(() => {
  bridge = mockBridge();
});
afterEach(() => vi.unstubAllGlobals());

describe("provenance drawer", () => {
  const recorded = edition({
    provenance: {
      run_id: "run-42",
      ran_as: "u-alice",
      started_at: "2026-11-22T08:00:00Z",
      tool_calls: [
        { tool: "crm_query", at: "2026-11-22T08:00:05Z", ok: true },
        { tool: "sales_report", at: "2026-11-22T08:00:09Z", ok: false },
      ],
    },
  });

  it("shows the run, when, who it ran as, and each tool call", () => {
    withClient(<ProvenanceDrawer edition={recorded} members={room().members} open onOpenChange={vi.fn()} />);
    const drawer = screen.getByTestId("provenance-drawer");
    expect(screen.getByTestId("provenance-run")).toHaveTextContent("run-42");
    expect(screen.getByTestId("provenance-when")).toHaveTextContent(/22 Nov/);
    expect(screen.getByTestId("provenance-ran-as")).toHaveTextContent("Alice");
    expect(screen.getByTestId("provenance-tool-0")).toHaveTextContent("crm_query");
    expect(screen.getByTestId("provenance-tool-0")).toHaveTextContent(/ok/i);
    expect(screen.getByTestId("provenance-tool-1")).toHaveTextContent("sales_report");
    expect(screen.getByTestId("provenance-tool-1")).toHaveTextContent(/failed/i);
    expect(drawer).toHaveTextContent(/recorded by the server/i);
  });

  it("names someone it ran as who is no longer a member by what the server sent", () => {
    const e = edition({ provenance: { run_id: "r", ran_as: { userId: "u-x", displayName: "Xavier" }, started_at: null, tool_calls: [] } });
    withClient(<ProvenanceDrawer edition={e} members={[]} open onOpenChange={vi.fn()} />);
    expect(screen.getByTestId("provenance-ran-as")).toHaveTextContent("Xavier");
    expect(screen.getByTestId("provenance-tools-empty")).toBeInTheDocument();
  });

  it("says so when an edition has no recorded provenance", () => {
    withClient(<ProvenanceDrawer edition={edition({ provenance: null })} members={[]} open onOpenChange={vi.fn()} />);
    expect(screen.getByTestId("provenance-missing")).toBeInTheDocument();
  });
});

describe("notification choices (C7)", () => {
  it("offers a linked external channel and saves the user's choices", async () => {
    bridge.on("GET", `/rooms/${REPORT_ID}/notify`, {
      inApp: true,
      muted: false,
      channels: { telegram: false },
      linkedChannels: [{ name: "telegram", label: "Telegram" }],
    });
    bridge.on("PUT", `/rooms/${REPORT_ID}/notify`, (body: unknown) => reply(body));
    withClient(<RoomNotifyDialog roomId={REPORT_ID} open onOpenChange={vi.fn()} />);
    const channel = await screen.findByTestId("notify-channel-telegram");
    expect(channel).not.toBeChecked();
    fireEvent.click(channel);
    fireEvent.click(screen.getByTestId("notify-save"));
    await waitFor(() => expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/notify`)).toHaveLength(1));
    expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/notify`)[0].body).toEqual({
      inApp: true,
      muted: false,
      channels: { telegram: true },
    });
    expect(await screen.findByTestId("notify-saved")).toBeInTheDocument();
  });

  it("does not offer a channel the user has not linked", async () => {
    bridge.on("GET", `/rooms/${REPORT_ID}/notify`, { inApp: true, muted: false, channels: { telegram: true }, linkedChannels: [] });
    withClient(<RoomNotifyDialog roomId={REPORT_ID} open onOpenChange={vi.fn()} />);
    expect(await screen.findByTestId("notify-no-channels")).toBeInTheDocument();
    expect(screen.queryByTestId("notify-channel-telegram")).toBeNull();
    // A choice for a channel that is not linked now is kept, not erased, on save.
    bridge.on("PUT", `/rooms/${REPORT_ID}/notify`, (body: unknown) => reply(body));
    fireEvent.click(screen.getByTestId("notify-save"));
    await waitFor(() =>
      expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/notify`)[0]?.body).toMatchObject({ channels: { telegram: true } }),
    );
  });

  it("mutes, and says the unread badge still shows", async () => {
    bridge.on("GET", `/rooms/${REPORT_ID}/notify`, { inApp: true, muted: false, channels: {}, linkedChannels: [] });
    bridge.on("PUT", `/rooms/${REPORT_ID}/notify`, (body: unknown) => reply(body));
    withClient(<RoomNotifyDialog roomId={REPORT_ID} open onOpenChange={vi.fn()} />);
    const mute = await screen.findByTestId("notify-mute");
    fireEvent.click(mute);
    expect(screen.getByTestId("notify-dialog")).toHaveTextContent(/unread count still shows/i);
    expect(screen.getByTestId("notify-in-app")).toBeDisabled();
    fireEvent.click(screen.getByTestId("notify-save"));
    await waitFor(() => expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/notify`)[0]?.body).toMatchObject({ muted: true }));
  });

  it("reports a read it could not make instead of showing defaults", async () => {
    bridge.on("GET", `/rooms/${REPORT_ID}/notify`, () => reply({ detail: "down" }, 503));
    withClient(<RoomNotifyDialog roomId={REPORT_ID} open onOpenChange={vi.fn()} />);
    expect(await screen.findByTestId("notify-error")).toHaveTextContent("503");
    expect(screen.queryByTestId("notify-save")).toBeNull();
  });
});
