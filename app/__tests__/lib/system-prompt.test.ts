import { describe, it, expect } from "vitest";
import { getVisualCanvasPrompt, ROBOTHOR_SYSTEM_PROMPT } from "@/lib/system-prompt";

describe("System Prompt", () => {
  it("includes Robothor identity", () => {
    expect(ROBOTHOR_SYSTEM_PROMPT).toContain("Robothor");
  });

  it("mentions autonomy principle", () => {
    expect(ROBOTHOR_SYSTEM_PROMPT).toContain("never suggest Philip do something manually");
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
    expect(prompt.length).toBeGreaterThan(100);
  });

  it("includes DASHBOARD marker format", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("[DASHBOARD:");
  });

  it("includes RENDER marker format", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("[RENDER:");
  });

  it("mentions triage agent decides independently", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("triage agent");
    expect(prompt).toContain("decides independently");
  });

  it("mentions markers are optional hints", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("optional");
    expect(prompt).toContain("hint");
  });

  it("lists available components", () => {
    const prompt = getVisualCanvasPrompt();
    expect(prompt).toContain("render_contact_table");
    expect(prompt).toContain("render_service_health");
    expect(prompt).toContain("render_memory_search");
  });
});
