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

// The fleet's health list is for operators: a member reads nothing there. The
// dialog's choices come from the workspace agent list instead.
vi.mock("@/hooks/use-agents", () => ({ useAgents: () => ({ agents: [] }) }));

const AGENTS = { agents: [{ agentId: "main", name: "Main" }, { agentId: "researcher", name: "Researcher" }] };
let agentsStatus: number;

let fetchMock: ReturnType<typeof vi.fn>;
let push: ReturnType<typeof vi.spyOn>;
let roomsStatus: number;

/** The rooms list (which says whether workspaces are served here), then the call under test. */
function bridge(answer: () => Response) {
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const get = (init?.method ?? "GET") === "GET";
    if (get && String(input) === "/api/bridge/api/rooms")
      return reply(roomsStatus === 200 ? { rooms: [] } : { detail: "Not Found" }, roomsStatus);
    if (get && String(input) === "/api/bridge/api/rooms/agents")
      return reply(agentsStatus === 200 ? AGENTS : { detail: { error: "agents_unavailable" } }, agentsStatus);
    return answer();
  });
}

/** The writes only: what the buttons sent, without the rooms list read. */
function posts() {
  return fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST");
}

beforeEach(() => {
  roomsStatus = 200;
  agentsStatus = 200;
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  bridge(() => reply({ error: "unexpected" }, 500));
  push = vi.spyOn(window.history, "pushState");
});
afterEach(() => {
  vi.unstubAllGlobals();
  push.mockRestore();
});

describe("OpenAsWorkspace (E6)", () => {
  it("opens the task's workspace and goes there", async () => {
    bridge(() => reply(room({ id: TASK_ID, kind: "task" })));
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(await screen.findByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${TASK_ID}`));
    expect(posts()[0][0]).toBe(`/api/bridge/api/rooms/from-task/${TASK_ACCEPTED}`);
  });

  it("lands a second opener in the same workspace", async () => {
    bridge(() => reply(room({ id: TASK_ID, kind: "task" })));
    const { unmount } = render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(await screen.findByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    unmount();
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(await screen.findByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    await waitFor(() => expect(push).toHaveBeenCalledTimes(2));
    expect(push.mock.calls[0][2]).toBe(push.mock.calls[1][2]);
  });

  it("says why it could not open, and stays put", async () => {
    bridge(() => reply({ detail: "task not found" }, 404));
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    fireEvent.click(await screen.findByTestId(`open-as-workspace-${TASK_ACCEPTED}`));
    expect(await screen.findByTestId(`open-as-workspace-error-${TASK_ACCEPTED}`)).toHaveTextContent("task not found");
    expect(push).not.toHaveBeenCalled();
  });
});

describe("NewWorkspaceButton (F1)", () => {
  it("creates an agent workspace with the agent picked and a title, then opens it", async () => {
    bridge(() => reply(room({ id: AGENT_ID, kind: "agent" })));
    render(<NewWorkspaceButton />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    fireEvent.change(await screen.findByTestId("new-workspace-agent"), { target: { value: "researcher" } });
    fireEvent.change(screen.getByTestId("new-workspace-title"), { target: { value: "Market scan" } });
    fireEvent.click(screen.getByTestId("new-workspace-create"));
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, "", `/?v=chat&room=${AGENT_ID}`));
    const [url, init] = posts()[0];
    expect(url).toBe("/api/bridge/api/rooms");
    expect(JSON.parse(String(init.body))).toEqual({ kind: "agent", title: "Market scan", agentId: "researcher" });
  });

  it("offers the company's agents from the workspace agent list, the main one first picked", async () => {
    render(<NewWorkspaceButton />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    const select = await screen.findByTestId("new-workspace-agent");
    await waitFor(() => expect(select).toHaveValue("main"));
    expect([...select.querySelectorAll("option")].map((o) => o.textContent)).toEqual(["Main", "Researcher"]);
    expect(screen.getByTestId("new-workspace-create")).toBeEnabled();
  });

  it("says when the agents cannot be listed, and creates nothing", async () => {
    agentsStatus = 502;
    render(<NewWorkspaceButton />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    expect(await screen.findByTestId("new-workspace-error")).toHaveTextContent(/agents could not be listed/i);
    expect(screen.getByTestId("new-workspace-create")).toBeDisabled();
  });

  it("starts with the given agent and a default title", async () => {
    render(<NewWorkspaceButton agentId="researcher" agentName="Researcher" label="New workspace with this agent" />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    expect(await screen.findByTestId("new-workspace-agent")).toHaveValue("researcher");
    expect(screen.getByTestId("new-workspace-title")).toHaveValue("Researcher");
  });

  it("will not create a workspace with no title", async () => {
    render(<NewWorkspaceButton agentId="main" />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    fireEvent.change(await screen.findByTestId("new-workspace-title"), { target: { value: "   " } });
    expect(screen.getByTestId("new-workspace-create")).toBeDisabled();
  });

  it("says plainly when the caller's role cannot create workspaces, and offers no create", async () => {
    bridge(() => reply({ error: "insufficient scope" }, 403));
    render(<NewWorkspaceButton agentId="main" />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    fireEvent.change(await screen.findByTestId("new-workspace-title"), { target: { value: "Market scan" } });
    fireEvent.click(screen.getByTestId("new-workspace-create"));
    expect(await screen.findByTestId("new-workspace-error")).toHaveTextContent(
      "Your role can't create workspaces. Ask a company admin if you need one.",
    );
    expect(screen.getByTestId("new-workspace-create")).toBeDisabled();
    expect(screen.queryByText(/insufficient scope/)).toBeNull();
  });

  it("shows the refusal for an unknown or disallowed agent", async () => {
    bridge(() => reply({ detail: "agent not allowed" }, 403));
    render(<NewWorkspaceButton agentId="main" />);
    fireEvent.click(await screen.findByTestId("new-workspace-button"));
    fireEvent.click(await screen.findByTestId("new-workspace-create"));
    expect(await screen.findByTestId("new-workspace-error")).toHaveTextContent("agent not allowed");
    expect(push).not.toHaveBeenCalled();
  });
});

describe("where workspaces are not served (the rooms list answers 404)", () => {
  it("offers no Open as workspace, and sends nothing", async () => {
    roomsStatus = 404;
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/rooms", expect.anything()));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId(`open-as-workspace-${TASK_ACCEPTED}`)).toBeNull();
    expect(posts()).toEqual([]);
  });

  it("offers no New workspace", async () => {
    roomsStatus = 404;
    render(<NewWorkspaceButton agentId="main" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/bridge/api/rooms", expect.anything()));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByTestId("new-workspace-button")).toBeNull();
  });

  it("offers nothing before the list has said workspaces are served", () => {
    fetchMock.mockImplementation(() => new Promise(() => undefined));
    render(<OpenAsWorkspace taskId={TASK_ACCEPTED} />);
    expect(screen.queryByTestId(`open-as-workspace-${TASK_ACCEPTED}`)).toBeNull();
  });
});
