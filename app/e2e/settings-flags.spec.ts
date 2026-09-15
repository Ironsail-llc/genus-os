/**
 * Settings › Flags, end to end, with every bridge route intercepted in the
 * browser — this spec must never reach a real bridge, because the act under
 * test moves a guardrail's rung on a live instance.
 *
 * The two assertions that matter: the write goes to the CONTROLS route with
 * the operator's reason (which is what `feature_flag_audit.reason` answers
 * "who widened this, and why" from), and the verdict is RE-READ afterwards
 * rather than assumed — a flag stepped back from enforce stops enforcing
 * immediately, and a page that kept showing ENFORCING would be reporting a
 * guardrail that is no longer there.
 */
import { test, expect, type Page, type Route } from "@playwright/test";

const FLAGS_URL = "/?v=settings&s=flags";

const LADDER = ["off", "observe", "alert", "enforce"];
const BOOL = ["true", "false"];

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function governed(name: string, choices: string[], description: string) {
  return {
    name,
    env: name,
    field: `flags.${name.toLowerCase()}`,
    group: "flags",
    aliases: [],
    type: choices.includes("true") ? "bool" : "str",
    description,
    default: choices[0],
    secret: false,
    governed: true,
    restart_required: false,
    restart_units: ["robothor-engine"],
    since: "legacy",
    hot: true,
    enum: choices,
  };
}

const SCHEMA = {
  groups: [
    {
      id: "flags",
      label: "Flags",
      fields: [
        governed("ROBOTHOR_RBAC_MODE", LADDER, "Role checks on every tool call."),
        governed("ROBOTHOR_RIP_1_ENABLED", BOOL, "The first engine upgrade."),
        governed("ROBOTHOR_JUDGE_ENABLED", BOOL, "Grade a run after it finishes."),
      ],
    },
  ],
};

/**
 * Four keys per entry, as the route has answered since fix round 3 — and
 * `ROBOTHOR_JUDGE_ENABLED` is governed AND env-supplied AND `editable: true`,
 * because the flag store reads its operator row before `os.environ`.
 */
const VALUES = {
  values: {
    // Supplied by a variable on the box, and editable anyway — which is the
    // point of fix round 3. After the write it is `db`, and the row must say
    // so rather than keeping the layer the write superseded.
    ROBOTHOR_RBAC_MODE: { value: "enforce", source: "env", editable: true, reason: null },
    ROBOTHOR_RIP_1_ENABLED: { value: "false", source: "default", editable: true, reason: null },
    ROBOTHOR_JUDGE_ENABLED: { value: "false", source: "default", editable: true, reason: null },
  },
  pending_restart: [],
};

const VALUES_AFTER_WRITE = {
  ...VALUES,
  values: {
    ...VALUES.values,
    ROBOTHOR_RBAC_MODE: { value: "observe", source: "db", editable: true, reason: null },
  },
};

function control(name: string, value: string, valid: string[], status: string, message: string) {
  return {
    name,
    value,
    valid_values: valid,
    verdict: {
      status,
      message,
      last_fired: status === "ENFORCING" ? "2099-01-01T09:00:00+00:00" : null,
      count_7d: status === "ENFORCING" ? 12 : 0,
    },
  };
}

interface Recorded {
  patches: Array<{ url: string; body: Record<string, unknown> }>;
}

async function setupMocks(page: Page): Promise<Recorded> {
  const recorded: Recorded = { patches: [] };

  await page.route("**/api/auth/session", (route) =>
    json(route, {
      user: { name: "Operator", email: "operator@example.com" },
      role: "owner",
      expires: "2099-01-01T00:00:00.000Z",
    })
  );
  await page.route("**/api/chat/history", (route) => json(route, { messages: [] }));
  await page.route("**/api/chat/plan/status", (route) => json(route, { active: false, plan: null }));
  await page.route("**/api/chat/deep/status", (route) => json(route, { active: false, deep: null }));
  await page.route("**/api/session", (route) => json(route, {}));
  await page.route("**/api/health", (route) => json(route, { status: "ok", services: [] }));
  await page.route("**/api/events/stream*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache", Connection: "keep-alive" },
      body: "event: ping\ndata: {}\n\n",
    })
  );

  // Registered FIRST, so it is matched LAST: nothing addressed at the bridge
  // can fall through to the real one.
  await page.route("**/api/bridge/**", (route) =>
    json(route, { detail: "this route is not intercepted by the spec" }, 503)
  );

  await page.route("**/api/bridge/api/settings/schema", (route) => json(route, SCHEMA));
  await page.route("**/api/bridge/api/settings", (route) =>
    json(route, recorded.patches.length ? VALUES_AFTER_WRITE : VALUES)
  );

  // Registered before `/api/controls` so it is matched first: that path does
  // not glob across a `/`, and without this the write would fall through to
  // the 503 catch-all.
  await page.route("**/api/bridge/api/controls/*", (route) => {
    recorded.patches.push({
      url: route.request().url(),
      body: (route.request().postDataJSON() ?? {}) as Record<string, unknown>,
    });
    return json(route, { name: "ROBOTHOR_RBAC_MODE", value: "observe" });
  });

  await page.route("**/api/bridge/api/controls", (route) => {
    // After the write, the guardrail is observing — so its verdict changes.
    const moved = recorded.patches.length > 0;
    return json(route, [
      moved
        ? control(
            "ROBOTHOR_RBAC_MODE",
            "observe",
            LADDER,
            "UNPROVEN",
            "observing; nothing has been blocked since the change"
          )
        : control(
            "ROBOTHOR_RBAC_MODE",
            "enforce",
            LADDER,
            "ENFORCING",
            "last fired 2099-01-01 09:00 (12 events / 7d)"
          ),
      control("ROBOTHOR_RIP_1_ENABLED", "false", BOOL, "UNPROVEN", "disabled"),
      control(
        "ROBOTHOR_JUDGE_ENABLED",
        "false",
        BOOL,
        "INERT",
        "NEVER FIRED — this control cannot protect you."
      ),
    ]);
  });

  return recorded;
}

test.describe("Settings › Flags", () => {
  test("flips a guardrail with a reason, and the verdict follows", async ({ page }) => {
    const recorded = await setupMocks(page);
    await page.goto(FLAGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="settings-page-flags"]')).toBeVisible({
      timeout: 15000,
    });

    // The three headings, and the honesty rule: a control that has never fired
    // is a warning, not a checkmark.
    await expect(page.locator('[data-testid="flags-section-guardrails"]')).toBeVisible();
    await expect(page.locator('[data-testid="flags-section-ladders"]')).toBeVisible();
    await expect(
      page.locator('[data-testid="flag-verdict-ROBOTHOR_JUDGE_ENABLED"]')
    ).toHaveAttribute("data-status", "INERT");
    await expect(page.locator('[data-testid="flag-verdict-ROBOTHOR_RBAC_MODE"]')).toHaveAttribute(
      "data-status",
      "ENFORCING"
    );

    // No reason, no write.
    await page.locator('[data-testid="flag-value-ROBOTHOR_RBAC_MODE-observe"]').click();
    await expect(page.locator('[data-testid="flag-apply-ROBOTHOR_RBAC_MODE"]')).toBeDisabled();

    await page
      .locator('[data-testid="flag-reason-ROBOTHOR_RBAC_MODE"]')
      .fill("soak complete, stepping back to observe for a week");
    await page.locator('[data-testid="flag-apply-ROBOTHOR_RBAC_MODE"]').click();

    await expect(page.locator('[data-testid="flag-verdict-ROBOTHOR_RBAC_MODE"]')).toHaveAttribute(
      "data-status",
      "UNPROVEN"
    );
    await expect(page.locator('[data-testid="flag-ROBOTHOR_RBAC_MODE"]')).toContainText(
      "nothing has been blocked since the change"
    );

    // The layer is re-read with the verdict. A row asserting a fresh value
    // beside the layer that value superseded is two contradictory facts on the
    // screen whose purpose is saying what is true.
    await expect(page.locator('[data-testid="flag-source-ROBOTHOR_RBAC_MODE"]')).toHaveText(
      "flag store"
    );
    await expect(page.locator('[data-testid="flag-env-note-ROBOTHOR_RBAC_MODE"]')).toHaveCount(0);

    expect(recorded.patches).toHaveLength(1);
    expect(recorded.patches[0].url).toContain("/api/controls/ROBOTHOR_RBAC_MODE");
    expect(recorded.patches[0].body).toEqual({
      value: "observe",
      reason: "soak complete, stepping back to observe for a week",
    });
  });

  test("keeps every rung reachable on a 390 px screen", async ({ page }) => {
    await setupMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(FLAGS_URL, { waitUntil: "networkidle" });

    await expect(page.locator('[data-testid="settings-page-flags"]')).toBeVisible({
      timeout: 15000,
    });
    for (const rung of LADDER) {
      await expect(page.locator(`[data-testid="flag-value-ROBOTHOR_RBAC_MODE-${rung}"]`)).toBeVisible();
    }

    const worst = await page.evaluate(() => {
      let overflow = 0;
      let el: Element | null = document.querySelector('[data-testid="settings-page-flags"]');
      while (el) {
        overflow = Math.max(overflow, el.scrollWidth - el.clientWidth);
        el = el.parentElement;
      }
      return overflow;
    });
    expect(worst).toBeLessThanOrEqual(1);
  });
});
