import { describe, it, expect, vi } from "vitest";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
}));

import { getVisualCanvasPrompt, ROBOTHOR_SYSTEM_PROMPT } from "@/lib/system-prompt";

describe("System Prompt", () => {
  it("includes Robothor identity", () => {
    expect(ROBOTHOR_SYSTEM_PROMPT).toContain("Robothor");
  });

  it("mentions autonomy principle", () => {
    expect(ROBOTHOR_SYSTEM_PROMPT).toContain("never suggest");
    expect(ROBOTHOR_SYSTEM_PROMPT).toContain("do something manually");
  });

  it("mentions visual canvas", () => {
    expect(ROBOTHOR_SYSTEM_PROMPT).toContain("visual canvas");
  });

  it("is non-empty", () => {
    expect(ROBOTHOR_SYSTEM_PROMPT.length).toBeGreaterThan(100);
  });
});

describe("Visual Canvas Prompt", () => {
  it("returns a non-empty string", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt.length).toBeGreaterThan(50);
  });

  it("includes DASHBOARD marker format", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("[DASHBOARD:");
  });

  it("includes RENDER marker format", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("[RENDER:");
  });

  it("mentions visual canvas", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("visual canvas");
  });

  it("mentions auto-updates", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("auto-update");
  });
});
