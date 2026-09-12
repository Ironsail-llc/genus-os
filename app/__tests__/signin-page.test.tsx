/**
 * The sign-in page renders the password form from the BRIDGE's live answer.
 *
 * It used to read `GENUS_LOCAL_LOGIN` out of this process's environment, fixed
 * at boot. The first-run wizard turns local login on for the INSTANCE — in
 * config.yaml, which the dashboard does not read — so an operator who had just
 * created their account, restarted the dashboard, and come here still found a
 * page with nothing on it until someone also set an environment variable.
 *
 * `GET /api/auth/methods` is public and is the endpoint that already exists to
 * answer "which sign-in methods does this instance offer".
 */

import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({
  headers: async () => new Headers(),
}));
vi.mock("next/navigation", () => ({
  redirect: vi.fn(),
}));
vi.mock("@/lib/auth", () => ({
  oidcProviderConfigured: () => false,
  signIn: vi.fn(),
}));
vi.mock("@/lib/services/registry", () => ({
  getServiceUrl: () => "http://bridge.test:9100",
}));
vi.mock("@/components/local-signin-form", () => ({
  LocalSignInForm: () => <form data-testid="local-form" />,
}));

const SignInPage = (await import("@/app/signin/page")).default;

function stubMethods(payload: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        ({ ok, status: ok ? 200 : 404, json: async () => payload }) as Response,
    ),
  );
}

async function renderPage() {
  const element = await SignInPage({
    searchParams: Promise.resolve({}),
  } as Parameters<typeof SignInPage>[0]);
  render(element);
}

describe("the sign-in page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("renders the password form when the bridge offers local login", async () => {
    // Deliberately with the env var UNSET: the whole point is that the boot
    // constant is no longer what decides.
    vi.stubEnv("GENUS_LOCAL_LOGIN", "");
    stubMethods({ local: true, oidc: [], cloudflare_access: false });

    await renderPage();

    expect(screen.getByTestId("local-form")).toBeInTheDocument();
  });

  it("renders no password form when the bridge does not offer it", async () => {
    // Even with the env var set — the bridge is the authority, and a form over
    // a route that 404s helps nobody.
    vi.stubEnv("GENUS_LOCAL_LOGIN", "true");
    stubMethods({ local: false, oidc: [], cloudflare_access: false });

    await renderPage();

    expect(screen.queryByTestId("local-form")).not.toBeInTheDocument();
  });

  it("renders no password form when the bridge cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );

    await renderPage();

    expect(screen.queryByTestId("local-form")).not.toBeInTheDocument();
  });
});
