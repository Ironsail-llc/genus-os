/**
 * The chat's Tools dialog, against the shapes the bridge proxies from
 * `GET /api/admin/agents/{id}/tools` and `GET /api/admin/mcp/servers`.
 *
 * What it must show: the MCP servers with their health, the agent's tools in
 * their groups (counts, names on demand), the manifest's allow and deny lists,
 * and a way to the agent editor. It changes nothing itself: enable and disable
 * stay in the manifest, which is the one source of truth.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToolsPopover } from "../tools-popover";

const TOOLS = {
  agent: "example-agent",
  groups: {
    core: ["read_file", "write_file"],
    plugins: { "acme-tools": ["acme_probe"] },
    mcp: { "example-mcp": ["example_search"] },
    opt_in: ["claude_code_start"],
  },
  allowed: ["read_file", "write_file", "acme_probe", "example_search", "claude_code_start"],
  denied: ["exec"],
  count: 5,
};

const SERVERS = {
  servers: [
    { name: "example-mcp", transport: "stdio", running: true, initialized: true, protocol: "2025-06-18", tool_count: 1 },
    { name: "remote-mcp", transport: "http", running: true, initialized: false, protocol: "2025-06-18", tool_count: 0 },
    { name: "idle-mcp", transport: "stdio", running: false, initialized: false, protocol: "2025-06-18", tool_count: 0 },
  ],
};

const TOOLS_URL = "/api/bridge/api/agents/example-agent/tools";
const SERVERS_URL = "/api/bridge/api/mcp/servers";

function installFetch(replies: Record<string, { status?: number; body: unknown }> = {}) {
  const table: Record<string, { status?: number; body: unknown }> = {
    [TOOLS_URL]: { body: TOOLS },
    [SERVERS_URL]: { body: SERVERS },
    ...replies,
  };
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const reply = table[String(input)] ?? { status: 404, body: { detail: "no route" } };
    const status = reply.status ?? 200;
    return { ok: status < 400, status, json: async () => reply.body } as Response;
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function openDialog(mobile = false) {
  const fetchMock = installFetch();
  render(<ToolsPopover agentId="example-agent" mobile={mobile} />);
  fireEvent.click(screen.getByRole("button", { name: "Tools and MCP servers" }));
  const dialog = await screen.findByRole("dialog");
  await within(dialog).findByTestId("tools-groups");
  return { dialog, fetchMock };
}

describe("ToolsPopover — the trigger", () => {
  it("is a labelled button and fetches nothing until opened", () => {
    const fetchMock = installFetch();
    render(<ToolsPopover agentId="example-agent" />);

    expect(screen.getByRole("button", { name: "Tools and MCP servers" })).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is a labelled chip on mobile", () => {
    installFetch();
    render(<ToolsPopover agentId="example-agent" mobile />);

    const chip = screen.getByRole("button", { name: "Tools and MCP servers" });
    expect(chip).toHaveTextContent("Tools");
  });
});

describe("ToolsPopover — the dialog", () => {
  it("reads this agent's tools and the MCP servers from the bridge", async () => {
    const { fetchMock } = await openDialog();

    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls).toContain(TOOLS_URL);
    expect(urls).toContain(SERVERS_URL);
  });

  it("lists each MCP server with its health and tool count", async () => {
    const { dialog } = await openDialog();

    const running = within(dialog).getByTestId("mcp-server-example-mcp");
    expect(running).toHaveTextContent("example-mcp");
    expect(running).toHaveTextContent("Ready");
    expect(running).toHaveTextContent("1 tool");
    expect(within(dialog).getByTestId("mcp-server-remote-mcp")).toHaveTextContent("Starting");
    expect(within(dialog).getByTestId("mcp-server-idle-mcp")).toHaveTextContent("Stopped");
  });

  it("counts the tools in each group and shows the names when a group is opened", async () => {
    const { dialog } = await openDialog();

    expect(within(dialog).getByTestId("tools-total")).toHaveTextContent("5 tools");
    const core = within(dialog).getByTestId("tool-group-core");
    expect(core).toHaveTextContent("Core");
    expect(core).toHaveTextContent("2");
    expect(within(dialog).getByTestId("tool-group-plugin-acme-tools")).toHaveTextContent("acme-tools");
    expect(within(dialog).getByTestId("tool-group-mcp-example-mcp")).toHaveTextContent("example-mcp");
    expect(within(dialog).getByTestId("tool-group-opt-in")).toHaveTextContent("Opt-in");

    fireEvent.click(within(core).getByText("Core"));
    expect(within(core).getByText("read_file")).toBeVisible();
    expect(within(core).getByText("write_file")).toBeVisible();
  });

  it("summarises the manifest's allow and deny lists", async () => {
    const { dialog } = await openDialog();

    const summary = within(dialog).getByTestId("tools-policy");
    expect(summary).toHaveTextContent("5 tools allowed by name");
    expect(summary).toHaveTextContent("exec");
  });

  it("says an agent with no allow list gets every tool that is not opt-in", async () => {
    installFetch({ [TOOLS_URL]: { body: { ...TOOLS, allowed: [], denied: [] } } });
    render(<ToolsPopover agentId="example-agent" />);
    fireEvent.click(screen.getByRole("button", { name: "Tools and MCP servers" }));

    const summary = await screen.findByTestId("tools-policy");
    expect(summary).toHaveTextContent("No allow list");
    expect(summary).toHaveTextContent("Nothing denied");
  });

  it("links to the agent editor for this agent", async () => {
    const { dialog } = await openDialog();

    const link = within(dialog).getByRole("link", { name: "Manage in agent settings" });
    expect(link).toHaveAttribute("href", "/?v=agents&agent=example-agent");
  });

  it("tells a non-operator the list is the operator's", async () => {
    installFetch({ [TOOLS_URL]: { status: 403, body: { detail: "operator role required" } } });
    render(<ToolsPopover agentId="example-agent" />);
    fireEvent.click(screen.getByRole("button", { name: "Tools and MCP servers" }));

    expect(await screen.findByTestId("tools-refused")).toHaveTextContent(/operator/i);
  });

  it("says so when the engine cannot be reached", async () => {
    installFetch({ [SERVERS_URL]: { status: 502, body: { error: "engine unavailable" } } });
    render(<ToolsPopover agentId="example-agent" />);
    fireEvent.click(screen.getByRole("button", { name: "Tools and MCP servers" }));

    expect(await screen.findByTestId("tools-error")).toBeVisible();
  });

  it("opens as a sheet on mobile with the same content", async () => {
    const { dialog } = await openDialog(true);

    expect(within(dialog).getByTestId("mcp-server-example-mcp")).toBeVisible();
    await waitFor(() =>
      expect(within(dialog).getByRole("link", { name: "Manage in agent settings" })).toBeVisible()
    );
  });
});
