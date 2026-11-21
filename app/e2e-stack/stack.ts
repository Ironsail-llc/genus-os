/**
 * The real stack's people and two ways in: the browser (sign in through the
 * dashboard's own form) and the bridge API (for setting up a step's world, or
 * checking it, without driving every click). Both use the same accounts.
 */
import { readFileSync } from "node:fs";
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

export type Name = "alice" | "bob" | "vera" | "dana" | "carol" | "hal" | "owen";
export interface Person {
  id: string;
  tenant: string;
  role: string;
  name: string;
  email: string;
}

const STACK_URL = process.env.ROOMS_STACK_URL;
const BRIDGE = process.env.ROOMS_STACK_BRIDGE_URL ?? "http://127.0.0.1:9171";
const PASSWORD = process.env.ROOMS_STACK_PASSWORD ?? "";

/** Skip the whole file unless the stack is up (`rooms-stack.sh env`). */
export function requireStack() {
  test.skip(!STACK_URL || !process.env.ROOMS_STACK_PEOPLE, "the rooms stack is not running (scripts/dev/rooms-stack.sh up)");
}

export function people(): Record<Name, Person> & { company: string; holding: string } {
  const data = JSON.parse(readFileSync(process.env.ROOMS_STACK_PEOPLE ?? "", "utf8"));
  return { ...data.people, company: data.company, holding: data.holding };
}

/** A browser context signed in as `who`, through the sign-in form. */
export async function signedIn(browser: Browser, who: Name, viewport?: { width: number; height: number }): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext(viewport ? { viewport } : {});
  const page = await context.newPage();
  await page.goto("/signin");
  await page.locator("#local-email").first().fill(`${who}@example.com`);
  await page.locator("#local-password").first().fill(PASSWORD);
  await page.locator('form button[type="submit"]').first().click();
  await page.waitForURL((url) => !url.pathname.startsWith("/signin"), { timeout: 30000 });
  return { context, page };
}

/**
 * One sign-in per person per run: the bridge rate-limits sign-in by address,
 * as it should, so a token is kept rather than asked for on every call.
 */
const tokens = new Map<Name, string>();

async function tokenFor(who: Name): Promise<string> {
  const kept = tokens.get(who);
  if (kept) return kept;
  const login = await fetch(`${BRIDGE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: `${who}@example.com`, password: PASSWORD }),
  });
  expect(login.status, `sign-in for ${who}`).toBe(200);
  const body = await login.json();
  const token: string = body.access_token ?? body.accessToken;
  tokens.set(who, token);
  return token;
}

/** The bridge API as `who`, with a token from the bridge's own sign-in. */
export async function api(who: Name) {
  let token = await tokenFor(who);
  const call = async (method: string, path: string, json?: unknown) => {
    const res = await fetch(`${BRIDGE}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(json === undefined ? {} : { "Content-Type": "application/json" }) },
      body: json === undefined ? undefined : JSON.stringify(json),
    });
    const text = await res.text();
    let data: unknown = text;
    try {
      data = JSON.parse(text);
    } catch {
      /* not JSON */
    }
    return { status: res.status, body: data as Record<string, unknown> & { [k: string]: unknown } };
  };
  return {
    call,
    get: (path: string) => call("GET", path),
    post: (path: string, json?: unknown) => call("POST", path, json ?? {}),
    patch: (path: string, json: unknown) => call("PATCH", path, json),
    put: (path: string, json?: unknown) => call("PUT", path, json ?? {}),
    /** Act in a descendant company, as the tenant switcher does. */
    async switchTo(tenant: string) {
      const res = await call("POST", "/api/auth/switch-tenant", { tenant });
      expect(res.status, `switch to ${tenant}`).toBe(200);
      token = (res.body.access_token ?? res.body.accessToken) as string;
    },
  };
}

/** A unique title, so specs never collide with each other's workspaces. */
export function unique(title: string): string {
  return `${title} ${Date.now().toString(36)}`;
}

export function roomLink(roomId: string): string {
  return `/?v=chat&room=${roomId}`;
}

/**
 * Send a message from a chat composer: the open workspace's, or with
 * `{ main: true }` the main conversation's (the page mounts both).
 */
export async function say(page: Page, text: string, where: { main?: boolean } = {}) {
  const input = where.main
    ? page.getByTestId("chat-input").locator("visible=true").first()
    : page.getByTestId("room-view").getByTestId("chat-input");
  await input.fill(text);
  await input.press("Enter");
}

/**
 * Mark a step that needs work still being built elsewhere: it runs, and is
 * expected to fail until that work merges, then this line is removed.
 */
export function expectedToFailUntil(reason: string) {
  test.fail(true, `expected to fail until merged: ${reason}`);
}
