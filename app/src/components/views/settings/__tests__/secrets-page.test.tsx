/**
 * Settings › Secrets — which credentials this instance holds, where each one
 * sits, when it last changed and which services read it. Never a value.
 *
 * What the suite pins:
 *
 * * every row the bridge lists is rendered with its source, last change and
 *   readers;
 * * a credential last changed more than `ROTATION_DAYS` ago is flagged, and
 *   one changed inside the window is not — tested on both sides of the line;
 * * a credential with no recorded change says so instead of looking fresh;
 * * a 403 is a sentence about access, not a red error banner;
 * * an unreachable bridge or a refusal is an error state, never an empty table.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ROTATION_DAYS, SecretsPage } from "../secrets-page";

const DAY = 24 * 60 * 60 * 1000;

function daysAgo(days: number): string {
  return new Date(Date.now() - days * DAY).toISOString();
}

function secret(name: string, overrides: Record<string, unknown> = {}) {
  return {
    name,
    configured: true,
    source: "vault",
    updated_at: daysAgo(3),
    readers: ["robothor-engine"],
    shadowed: false,
    shadowed_by: null,
    env_name: name,
    vault_readable: true,
    ...overrides,
  };
}

function reply(status: number, body: unknown): Response {
  return { ok: status < 400, status, json: async () => body } as Response;
}

function mockSecrets(status: number, body: unknown) {
  return vi.spyOn(global, "fetch").mockResolvedValue(reply(status, body));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SecretsPage", () => {
  it("names the rotation window it flags against", () => {
    expect(ROTATION_DAYS).toBe(90);
  });

  it("lists every secret with its source, last change and readers", async () => {
    mockSecrets(200, {
      secrets: [
        secret("EXAMPLE_API_TOKEN", { readers: ["robothor-engine", "robothor-bridge"] }),
        secret("EXAMPLE_WEBHOOK_URL", { source: "env", updated_at: null }),
        secret("EXAMPLE_MISSING_TOKEN", {
          configured: false,
          source: "none",
          updated_at: null,
        }),
      ],
    });
    render(<SecretsPage visible role="owner" />);

    const row = await screen.findByTestId("secrets-row-EXAMPLE_API_TOKEN");
    expect(row).toHaveTextContent("EXAMPLE_API_TOKEN");
    expect(row).toHaveTextContent(/vault/i);
    expect(row).toHaveTextContent("robothor-engine");
    expect(row).toHaveTextContent("robothor-bridge");
    expect(within(row).getByTestId("secrets-configured-EXAMPLE_API_TOKEN")).toHaveTextContent(
      /configured/i
    );

    const missing = screen.getByTestId("secrets-row-EXAMPLE_MISSING_TOKEN");
    expect(missing).toHaveTextContent(/not configured/i);

    expect(screen.getByTestId("secrets-row-EXAMPLE_WEBHOOK_URL")).toHaveTextContent(
      /never recorded/i
    );
    expect(global.fetch).toHaveBeenCalledWith("/api/bridge/api/secrets");
  });

  it("flags a secret last changed 91 days ago as rotation overdue", async () => {
    mockSecrets(200, { secrets: [secret("EXAMPLE_OLD_TOKEN", { updated_at: daysAgo(91) })] });
    render(<SecretsPage visible role="owner" />);
    const row = await screen.findByTestId("secrets-row-EXAMPLE_OLD_TOKEN");
    expect(within(row).getByTestId("secrets-overdue-EXAMPLE_OLD_TOKEN")).toHaveTextContent(
      /rotation overdue/i
    );
  });

  it("does not flag a secret last changed 89 days ago", async () => {
    mockSecrets(200, { secrets: [secret("EXAMPLE_FRESH_TOKEN", { updated_at: daysAgo(89) })] });
    render(<SecretsPage visible role="owner" />);
    const row = await screen.findByTestId("secrets-row-EXAMPLE_FRESH_TOKEN");
    expect(within(row).queryByTestId("secrets-overdue-EXAMPLE_FRESH_TOKEN")).toBeNull();
    expect(row).not.toHaveTextContent(/overdue/i);
  });

  it("does not flag a secret exactly 90 days old: overdue means MORE than 90", async () => {
    mockSecrets(200, { secrets: [secret("EXAMPLE_EDGE_TOKEN", { updated_at: daysAgo(90) })] });
    render(<SecretsPage visible role="owner" />);
    const row = await screen.findByTestId("secrets-row-EXAMPLE_EDGE_TOKEN");
    expect(within(row).queryByTestId("secrets-overdue-EXAMPLE_EDGE_TOKEN")).toBeNull();
  });

  it("says the vault could not be read instead of calling its secrets missing", async () => {
    mockSecrets(200, {
      secrets: [
        secret("EXAMPLE_VAULT_TOKEN", {
          configured: false,
          source: "none",
          updated_at: null,
          vault_readable: false,
        }),
      ],
    });
    render(<SecretsPage visible role="owner" />);
    const row = await screen.findByTestId("secrets-row-EXAMPLE_VAULT_TOKEN");
    expect(screen.getByTestId("secrets-vault-unreadable")).toHaveTextContent(
      /vault could not be read/i
    );
    expect(within(row).getByTestId("secrets-configured-EXAMPLE_VAULT_TOKEN")).toHaveTextContent(
      /unknown/i
    );
    expect(row).not.toHaveTextContent(/not configured/i);
    expect(row).not.toHaveTextContent(/nowhere/i);
  });

  it("raises no vault alert when the vault was read", async () => {
    mockSecrets(200, { secrets: [secret("EXAMPLE_API_TOKEN")] });
    render(<SecretsPage visible role="owner" />);
    await screen.findByTestId("secrets-row-EXAMPLE_API_TOKEN");
    expect(screen.queryByTestId("secrets-vault-unreadable")).toBeNull();
  });

  it("reads the list when the page becomes visible", async () => {
    const fetchSpy = mockSecrets(200, { secrets: [secret("EXAMPLE_API_TOKEN")] });
    const { rerender } = render(<SecretsPage visible={false} role="owner" />);
    expect(fetchSpy).not.toHaveBeenCalled();
    rerender(<SecretsPage visible role="owner" />);
    expect(await screen.findByTestId("secrets-row-EXAMPLE_API_TOKEN")).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("states the rotation window and where secrets are set", async () => {
    mockSecrets(200, { secrets: [secret("EXAMPLE_API_TOKEN")] });
    render(<SecretsPage visible role="owner" />);
    await screen.findByTestId("secrets-row-EXAMPLE_API_TOKEN");
    const page = screen.getByTestId("settings-page-secrets");
    expect(page).toHaveTextContent(`${ROTATION_DAYS} days`);
    expect(page).toHaveTextContent("genus vault set");
  });

  it("says so when the instance holds no secrets", async () => {
    mockSecrets(200, { secrets: [] });
    render(<SecretsPage visible role="owner" />);
    expect(await screen.findByTestId("secrets-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("secrets-table")).toBeNull();
  });

  it("tells a caller the bridge refuses that operator access is required", async () => {
    mockSecrets(403, { detail: "operator role required" });
    render(<SecretsPage visible role="owner" />);
    const forbidden = await screen.findByTestId("secrets-forbidden");
    expect(forbidden).toHaveTextContent(/operator access required/i);
    expect(screen.queryByTestId("secrets-error")).toBeNull();
    expect(screen.queryByTestId("secrets-table")).toBeNull();
  });

  it("shows a viewer the access message without asking the bridge", () => {
    const fetchSpy = mockSecrets(200, { secrets: [] });
    render(<SecretsPage visible role="viewer" />);
    expect(screen.getByTestId("secrets-forbidden")).toHaveTextContent(
      /operator access required/i
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("shows an error, not an empty table, when the bridge fails", async () => {
    mockSecrets(500, { detail: "the vault could not be read" });
    render(<SecretsPage visible role="owner" />);
    const error = await screen.findByTestId("secrets-error");
    expect(error).toHaveTextContent("the vault could not be read");
    expect(screen.queryByTestId("secrets-empty")).toBeNull();
  });

  it("shows an error when the bridge cannot be reached", async () => {
    vi.spyOn(global, "fetch").mockRejectedValue(new Error("down"));
    render(<SecretsPage visible role="owner" />);
    expect(await screen.findByTestId("secrets-error")).toBeInTheDocument();
  });

  it("shows a loading state until the list arrives", async () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise<Response>(() => {}));
    render(<SecretsPage visible role="owner" />);
    expect(screen.getByTestId("secrets-loading")).toBeInTheDocument();
  });

  it("asks nothing while not visible", async () => {
    const fetchSpy = mockSecrets(200, { secrets: [] });
    render(<SecretsPage visible={false} role="owner" />);
    await waitFor(() => expect(fetchSpy).not.toHaveBeenCalled());
  });
});
