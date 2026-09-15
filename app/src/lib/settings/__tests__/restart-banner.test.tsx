/**
 * The restart banner's memory, and why it is not component state.
 *
 * `GET /api/settings`'s `pending_restart` answers a different question from
 * `PATCH /api/settings`'s: the GET's list is "config.yaml is being ignored by
 * this process right now", the PATCH's is "the units YOUR change needs". Only
 * the second belongs in a post-save banner, and the route keeps no ledger of
 * it — so the memory is the client's, and it has to outlive the component.
 *
 * The Settings container UNMOUNTS an inactive page (see `settings-view.tsx`),
 * so a `useState` in the Config page would lose the banner the moment the
 * operator looked at another page — which is exactly when they would forget
 * that a service still needs restarting.
 */
import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import {
  dismissRestartNotice,
  noteRestartNeeded,
  resetRestartNotice,
  restartNotice,
  useRestartNotice,
} from "@/lib/settings/restart-banner";

function Probe() {
  const notice = useRestartNotice();
  if (!notice) return <div data-testid="probe">none</div>;
  return (
    <div data-testid="probe">
      {notice.units.join(", ")} / {notice.names.join(", ")}
    </div>
  );
}

beforeEach(() => {
  resetRestartNotice();
});

describe("the restart notice", () => {
  it("starts with nothing to say", () => {
    expect(restartNotice()).toBeNull();
  });

  it("remembers the units a save reported, and what was saved", () => {
    noteRestartNeeded(["robothor-engine"], ["ROBOTHOR_LOG_DIR"]);
    expect(restartNotice()).toEqual({
      units: ["robothor-engine"],
      names: ["ROBOTHOR_LOG_DIR"],
    });
  });

  it("ignores a save that needs no restart, rather than showing an empty banner", () => {
    noteRestartNeeded([], ["ROBOTHOR_RBAC_MODE"]);
    expect(restartNotice()).toBeNull();
  });

  it("unions a second save into the first, sorted, without repeating a unit", () => {
    noteRestartNeeded(["robothor-engine"], ["ROBOTHOR_LOG_DIR"]);
    noteRestartNeeded(["robothor-bridge", "robothor-engine"], ["ROBOTHOR_BRIDGE_PORT"]);
    expect(restartNotice()).toEqual({
      units: ["robothor-bridge", "robothor-engine"],
      names: ["ROBOTHOR_BRIDGE_PORT", "ROBOTHOR_LOG_DIR"],
    });
  });

  it("is dismissable, and stays dismissed", () => {
    noteRestartNeeded(["robothor-engine"], ["ROBOTHOR_LOG_DIR"]);
    dismissRestartNotice();
    expect(restartNotice()).toBeNull();
  });

  it("survives the component that recorded it being unmounted and mounted again", () => {
    const first = render(<Probe />);
    act(() => noteRestartNeeded(["robothor-engine"], ["ROBOTHOR_LOG_DIR"]));
    expect(screen.getByTestId("probe")).toHaveTextContent("robothor-engine / ROBOTHOR_LOG_DIR");

    first.unmount();
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveTextContent("robothor-engine / ROBOTHOR_LOG_DIR");
  });

  it("tells a mounted subscriber when it is dismissed", () => {
    noteRestartNeeded(["robothor-engine"], ["ROBOTHOR_LOG_DIR"]);
    render(<Probe />);
    act(() => dismissRestartNotice());
    expect(screen.getByTestId("probe")).toHaveTextContent("none");
  });

  it("hands React the same snapshot until something changes", () => {
    // useSyncExternalStore re-renders forever on a snapshot that is a fresh
    // object each call. This is the assertion that keeps that from shipping.
    noteRestartNeeded(["robothor-engine"], ["ROBOTHOR_LOG_DIR"]);
    expect(restartNotice()).toBe(restartNotice());
  });
});
