/**
 * Sharing a workspace (G1, G7, F2): add and remove people of this company,
 * mark someone assigned, see who the workspace runs as, and a plain warning
 * that the people it is shared with see data fetched with that person's
 * access.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ShareDialog } from "@/components/rooms/share-dialog";
import { AGENT_ID, REPORT_ID, reply, room } from "@/lib/rooms/__tests__/fixtures";
import type { Room } from "@/lib/rooms/types";
import { mockBridge, withClient } from "./controls-harness";

let bridge: ReturnType<typeof mockBridge>;

const PEOPLE = { users: [ { userId: "u-alice", displayName: "Alice" }, { userId: "u-bob", displayName: "Bob" }, { userId: "u-carol", displayName: "Carol" } ] };

beforeEach(() => {
  bridge = mockBridge();
  bridge.on("GET", "/rooms/directory", PEOPLE);
});
afterEach(() => vi.unstubAllGlobals());

function dialog(r: Room = room()) {
  const onChanged = vi.fn();
  withClient(<ShareDialog room={r} open onOpenChange={vi.fn()} onChanged={onChanged} />);
  return { onChanged };
}

describe("share dialog", () => {
  it("says who it runs as and warns what sharing exposes (G7)", () => {
    dialog();
    expect(screen.getByTestId("share-runs-as")).toHaveTextContent("Runs as Alice");
    expect(screen.getByTestId("share-warning")).toHaveTextContent(
      /people you share this with see data fetched with Alice's access/i,
    );
  });

  it("lists the members and who is assigned", () => {
    dialog();
    expect(screen.getByTestId("share-member-u-alice")).toHaveTextContent("Alice");
    expect(screen.getByTestId("share-assigned-u-alice")).toBeChecked();
    expect(screen.getByTestId("share-assigned-u-bob")).not.toBeChecked();
  });

  it("offers only people of this company who are not already members", async () => {
    dialog();
    const select = (await screen.findByTestId("share-person")) as HTMLSelectElement;
    await waitFor(() => expect(within(select).getAllByRole("option").map((o) => o.textContent)).toContain("Carol"));
    const names = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(names).not.toContain("Alice");
    expect(names).not.toContain("Bob");
  });

  it("shares with a person, assigned when asked", async () => {
    bridge.on("PUT", `/rooms/${REPORT_ID}/members/u-carol`, room());
    const { onChanged } = dialog();
    const select = await screen.findByTestId("share-person");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    fireEvent.change(select, { target: { value: "u-carol" } });
    fireEvent.click(screen.getByTestId("share-add-assigned"));
    fireEvent.click(screen.getByTestId("share-add"));
    await waitFor(() => expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/members/u-carol`)).toHaveLength(1));
    expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/members/u-carol`)[0].body).toEqual({ assigned: true });
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("marks a member assigned, and removes one", async () => {
    bridge.on("PUT", `/rooms/${REPORT_ID}/members/u-bob`, room());
    bridge.on("DELETE", `/rooms/${REPORT_ID}/members/u-bob`, { ok: true });
    const { onChanged } = dialog();
    fireEvent.click(screen.getByTestId("share-assigned-u-bob"));
    await waitFor(() =>
      expect(bridge.calls("PUT", `/rooms/${REPORT_ID}/members/u-bob`)[0]?.body).toEqual({ assigned: true }),
    );
    fireEvent.click(screen.getByTestId("share-remove-u-bob"));
    await waitFor(() => expect(bridge.calls("DELETE", `/rooms/${REPORT_ID}/members/u-bob`)).toHaveLength(1));
    expect(onChanged).toHaveBeenCalledTimes(2);
  });

  it("shows the server's refusal, such as a person from another company (G1)", async () => {
    bridge.on("PUT", `/rooms/${REPORT_ID}/members/u-carol`, () =>
      reply({ detail: "That person is not in this company" }, 403),
    );
    dialog();
    const select = await screen.findByTestId("share-person");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    fireEvent.change(select, { target: { value: "u-carol" } });
    fireEvent.click(screen.getByTestId("share-add"));
    expect(await screen.findByTestId("share-error")).toHaveTextContent("That person is not in this company");
  });

  it("says plainly when this role cannot list people, and points at the agent", async () => {
    bridge.on("GET", "/rooms/directory", () => reply({ detail: "Forbidden" }, 403));
    dialog();
    expect(await screen.findByTestId("share-no-directory")).toHaveTextContent(/ask the agent/i);
  });

  it("works the same for an agent workspace, with no run-as line (F2)", () => {
    dialog(room({ id: AGENT_ID, kind: "agent", runsAs: undefined }));
    expect(screen.queryByTestId("share-runs-as")).toBeNull();
    expect(screen.getByTestId("share-member-u-bob")).toBeInTheDocument();
    expect(screen.getByTestId("share-remove-u-bob")).toBeInTheDocument();
  });
});
