import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

/**
 * The autonomy dashboard shipped to everyone.
 *
 * `/account/autonomy` and the link to it from `/account/security` were added
 * unconditionally, so every instance served a page offering to store payment
 * cards and hand an agent spending authority — on instances whose operator
 * had never enabled, or heard of, the feature. Both now key off
 * `ROBOTHOR_AUTONOMY_ENABLED`, and "off" is 404 rather than a disabled card.
 */

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

vi.mock("@/components/personal-automation-panel", () => ({
  PersonalAutomationPanel: () => <div>panel</div>,
}));

vi.mock("@/components/account-security-panel", () => ({
  AccountSecurityPanel: () => <div>security panel</div>,
}));

afterEach(() => {
  cleanup();
  delete process.env.ROBOTHOR_AUTONOMY_ENABLED;
  vi.resetModules();
});

async function autonomyPage() {
  return (await import("@/app/account/autonomy/page")).default;
}

async function securityPage() {
  return (await import("@/app/account/security/page")).default;
}

describe("the autonomy route", () => {
  it("is 404 on an instance that does not offer personal automation", async () => {
    const Page = await autonomyPage();
    expect(() => render(<Page />)).toThrow("NEXT_NOT_FOUND");
  });

  it("renders once the instance opts in", async () => {
    process.env.ROBOTHOR_AUTONOMY_ENABLED = "true";
    const Page = await autonomyPage();
    render(<Page />);
    expect(screen.getByText("Personal automation")).toBeTruthy();
    expect(screen.getByText("panel")).toBeTruthy();
  });
});

describe("the link from account security", () => {
  it("is absent on an instance that does not offer personal automation", async () => {
    const Page = await securityPage();
    render(<Page />);
    expect(screen.getByText("security panel")).toBeTruthy();
    expect(screen.queryByText("Personal information and delegated tasks")).toBeNull();
  });

  it("appears once the instance opts in", async () => {
    process.env.ROBOTHOR_AUTONOMY_ENABLED = "true";
    const Page = await securityPage();
    render(<Page />);
    expect(screen.getByText("Personal information and delegated tasks")).toBeTruthy();
  });
});
