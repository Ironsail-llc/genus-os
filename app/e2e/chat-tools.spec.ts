/**
 * The composer's Tools dialog, in the real browser.
 *
 * Every backend is intercepted. What this proves beyond the unit tests: the
 * button a person clicks asks the bridge about the agent the chat is talking
 * to (and follows the agent switcher), and the dialog renders the servers,
 * the groups and the link to that agent's editor.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const MANIFESTS = {
  default_agent: "main",
  count: 2,
  broken: [],
  agents: [
    { id: "main", name: "Example Assistant", chattable: true, session_target: "isolated" },
    { id: "example-agent", name: "Example Agent", chattable: true, session_target: "persistent" },
  ],
};

function tools(agent: string) {
  return {
    agent,
    groups: {
      core: ["read_file", "write_file"],
      plugins: { "acme-tools": ["acme_probe"] },
      mcp: { "example-mcp": ["example_search"] },
      opt_in: [],
    },
    allowed: [],
    denied: ["exec"],
    count: 4,
  };
}

const SERVERS = {
  servers: [
    { name: "example-mcp", transport: "stdio", running: true, initialized: true, protocol: "2025-06-18", tool_count: 1 },
  ],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function setupMocks(page: Page, asked: string[]) {
  await page.route("**/api/bridge/api/agent-manifests", (route) => json(route, MANIFESTS));
  await page.route("**/api/bridge/api/agents/*/tools", (route) => {
    const agent = new URL(route.request().url()).pathname.split("/").at(-2) ?? "";
    asked.push(agent);
    return json(route, tools(agent));
  });
  await page.route("**/api/bridge/api/mcp/servers", (route) => json(route, SERVERS));
  await page.route("**/api/chat/history*", (route) => json(route, { messages: [] }));
  await page.route("**/api/chat/plan/status*", (route) => json(route, { active: false }));
  await page.route("**/api/chat/deep/status*", (route) => json(route, { active: false }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/dashboard/welcome", (route) =>
    json(route, { html: "<div>Welcome</div>", type: "html" })
  );
  await page.route("**/api/dashboard/generate", (route) => route.fulfill({ status: 204, body: "" }));
  await page.route("**/api/health", (route) => json(route, { status: "healthy", agents: {} }));
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    })
  );
}

test.describe("Chat — what the agent can reach", () => {
  test("the Tools button lists the chat agent's tools and servers", async ({ page }) => {
    const asked: string[] = [];
    await setupMocks(page, asked);
    await page.goto("/", { waitUntil: "networkidle" });
    await expect(page.locator('[data-testid="chat-input"]')).toBeVisible({ timeout: 10000 });

    await expect(page.getByRole("button", { name: "Plan mode" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Deep mode" })).toBeVisible();
    await page.getByRole("button", { name: "Tools and MCP servers" }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog.getByTestId("mcp-server-example-mcp")).toContainText("Ready");
    await expect(dialog.getByTestId("tools-total")).toHaveText("4 tools");
    await dialog.getByTestId("tool-group-core").getByRole("button").click();
    await expect(dialog.getByText("write_file")).toBeVisible();
    await expect(dialog.getByTestId("tools-policy")).toContainText("exec");
    await expect(dialog.getByRole("link", { name: "Manage in agent settings" })).toHaveAttribute(
      "href",
      "/?v=agents&agent=main"
    );
    expect(asked).toEqual(["main"]);

    await page.keyboard.press("Escape");
    await page.locator('[data-testid="agent-switcher"]').selectOption("example-agent");
    await page.getByRole("button", { name: "Tools and MCP servers" }).click();
    await expect(page.getByRole("dialog").getByRole("link", { name: "Manage in agent settings" })).toHaveAttribute(
      "href",
      "/?v=agents&agent=example-agent"
    );
    expect(asked).toEqual(["main", "example-agent"]);
  });
});
