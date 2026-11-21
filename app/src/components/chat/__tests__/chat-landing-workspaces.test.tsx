/**
 * C1 on the chat landing: Bob signs in and the landing lists the workspaces
 * with something new, or says nothing is new. Choosing one opens it in place
 * (client-side, through the view route), not with a page load.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AGENT_ID, REPORT_ID, reply, summary } from "@/lib/rooms/__tests__/fixtures";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ pushView: vi.fn() }),
}));

import { ChatLanding } from "../chat-landing";

let roomsBody: unknown;

beforeEach(() => {
  roomsBody = {
    rooms: [
      summary({ id: REPORT_ID, title: "Weekly sales", unread: 2 }),
      summary({ id: AGENT_ID, kind: "agent", title: "Research desk", unread: 0 }),
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/bridge/api/rooms") return reply(roomsBody);
      return reply({});
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

function landing() {
  return render(<ChatLanding suggestedPrompts={[]} onPrompt={vi.fn()} />);
}

describe("ChatLanding — new in your workspaces", () => {
  it("lists the workspaces with new activity", async () => {
    landing();
    const list = await screen.findByTestId(`new-in-workspaces-item-${REPORT_ID}`);
    expect(list).toHaveTextContent("Weekly sales");
    expect(within(screen.getByTestId("new-in-workspaces")).queryByText("Research desk")).toBeNull();
  });

  it("says nothing is new when nothing is", async () => {
    roomsBody = { rooms: [summary({ unread: 0 })] };
    landing();
    expect(await screen.findByTestId("new-in-workspaces-empty")).toBeInTheDocument();
  });

  it("opens a workspace without a page load", async () => {
    const push = vi.spyOn(window.history, "pushState");
    landing();
    fireEvent.click(await screen.findByTestId(`new-in-workspaces-item-${REPORT_ID}`));
    expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${REPORT_ID}`);
    push.mockRestore();
  });
});
