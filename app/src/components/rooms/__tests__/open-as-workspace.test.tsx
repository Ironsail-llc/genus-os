/**
 * Entry points into workspaces:
 * - E6: "Open as workspace" on a task: `POST /api/rooms/from-task/{id}`, then
 *   into the room. A second opener gets the same room back, so lands there.
 * - F1: "New workspace" (kind `agent`), from the rail or from an agent; a
 *   refused agent is said plainly.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewWorkspaceButton, OpenAsWorkspace } from "@/components/rooms/open-as-workspace";
import { AGENT_ID, TASK_ID, reply, room } from "@/lib/rooms/__tests__/fixtures";
import { TASK_ACCEPTED } from "@/lib/rooms/__tests__/hub-fixtures";

vi.mock("@/hooks/use-agents", () => ({
  useAgents: () => ({
    agents: [
      { name: "Main", agentId: "main", status: "healthy", schedule: "" },
      { name: "Researcher", agentId: "researcher", status: "healthy", schedule: "" },
    ],
  }),
}));

let fetchMock: ReturnType<typeof vi.fn>;
let push: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  push = vi.spyOn(window.history, "pushState");
});
afterEach(() => {
  vi.unstubAllGlobals();
  push.mockRestore();
});

describe("OpenAsWorkspace (E6)", () => {
  it("opens the task's workspace and goes there", async () => {
    fetchMock.mockResolvedValue(reply(room({ id: TASK_ID, kind: "task" })));
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(screen.getByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${TASK_ID}`));
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/bridge/api/rooms/from-task/${TASK_ACCEPTED}`,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("lands a second opener in the same workspace", async () => {
    fetchMock.mockImplementation(async () => reply(room({ id: TASK_ID, kind: "task" })));
    const { unmount } = render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(screen.getByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    unmount();
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(screen.getByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    await waitFor(() => expect(push).toHaveBeenCalledTimes(2));
    expect(push.mock.calls[0][2]).toBe(push.mock.calls[1][2]);
  });

  it("says why it could not open, and stays put", async () => {
    fetchMock.mockResolvedValue(reply({ detail: "task not found" }, 404));
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(screen.getByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    expect(await screen.findByTestId(`open-as-workspace-error-${TASK_ACCEPTED}`)).toHaveTextContent("task not found");
    expect(push).not.toHaveBeenCalled();
  });
});

describe("NewWorkspaceButton (F1)", () => {
  it("creates an agent workspace with the agent picked and a title, then opens it", async () => {
    fetchMock.mockResolvedValue(reply(room({ id: AGENT_ID, kind: "agent" })));
    render(<NewWorkspaceButton />);
    fireEvent.click(screen.getByTestId("new-workspace-button"));
    fireEvent.change(await screen.findByTestId("new-workspace-agent"), { target: { value: "researcher" } });
    fireEvent.change(screen.getByTestId("new-workspace-title"), { target: { value: "Market scan" } });
    fireEvent.click(screen.getByTestId("new-workspace-create"));
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${AGENT_ID}`));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/bridge/api/rooms");
    expect(JSON.parse(String(init.body))).toEqual({ kind: "agent", title: "Market scan", agentId: "researcher" });
  });

  it("starts with the given agent and a default title", async () => {
    render(<NewWorkspaceButton agentId="researcher" agentName="Researcher" label="New workspace with this agent" />);
    fireEvent.click(screen.getByTestId("new-workspace-button"));
    expect(await screen.findByTestId("new-workspace-agent")).toHaveValue("researcher");
    expect(screen.getByTestId("new-workspace-title")).toHaveValue("Researcher");
  });

  it("will not create a workspace with no title", async () => {
    render(<NewWorkspaceButton agentId="main" />);
    fireEvent.click(screen.getByTestId("new-workspace-button"));
    fireEvent.change(await screen.findByTestId("new-workspace-title"), { target: { value: "   " } });
    expect(screen.getByTestId("new-workspace-create")).toBeDisabled();
  });

  it("shows the refusal for an unknown or disallowed agent", async () => {
    fetchMock.mockResolvedValue(reply({ detail: "agent not allowed" }, 403));
    render(<NewWorkspaceButton agentId="main" />);
    fireEvent.click(screen.getByTestId("new-workspace-button"));
    fireEvent.click(await screen.findByTestId("new-workspace-create"));
    expect(await screen.findByTestId("new-workspace-error")).toHaveTextContent("agent not allowed");
    expect(push).not.toHaveBeenCalled();
  });
});
