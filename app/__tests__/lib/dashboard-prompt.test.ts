import { describe, it, expect } from "vitest";
import { getDashboardSystemPrompt, getTimeAwarePrompt, buildEnrichedPrompt } from "@/lib/dashboard/system-prompt";
import { TEMPLATE_CATALOG, generateCatalogPrompt } from "@/lib/dashboard/template-catalog";

describe("Dashboard System Prompt", () => {
  it("is non-empty and substantial", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt.length).toBeGreaterThan(500);
  });

  it("instructs HTML output (no TSX)", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("HTML");
    expect(prompt).toContain("No markdown fences");
    expect(prompt).not.toContain("Prefer TSX");
    expect(prompt).not.toContain("export default");
  });

  it("includes dark theme styling rules", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("Tailwind");
    expect(prompt).toContain("Dark theme");
    expect(prompt).toContain("glass");
    expect(prompt).toContain("gradient-text");
  });

  it("includes data display patterns", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("Metric Card");
    expect(prompt).toContain("Service Status");
    expect(prompt).toContain("sparklineSVG");
    expect(prompt).toContain("animateValue");
  });

  it("includes Chart.js charting instructions", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("Chart.js");
    expect(prompt).toContain("new Chart");
    expect(prompt).toContain("Bar Chart");
    expect(prompt).toContain("Line Chart");
    expect(prompt).toContain("Doughnut Chart");
    expect(prompt).toContain("Gauge Chart");
    expect(prompt).toContain("Radar Chart");
    expect(prompt).toContain("createGradient");
    expect(prompt).toContain("datalabels");
  });

  it("includes bento grid layout", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("grid-cols-12");
    expect(prompt).toContain("col-span");
    expect(prompt).toContain("bento");
  });

  it("includes premium card patterns", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("glass");
    expect(prompt).toContain("accent");
    expect(prompt).toContain("border-l-2");
  });

  it("includes helper function references", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("createGradient");
    expect(prompt).toContain("animateValue");
    expect(prompt).toContain("sparklineSVG");
  });

  it("includes plugin references", () => {
    const prompt = getDashboardSystemPrompt();
    expect(prompt).toContain("datalabels");
    expect(prompt).toContain("annotation");
  });
});

describe("Template Catalog", () => {
  it("has 17 templates", () => {
    expect(TEMPLATE_CATALOG).toHaveLength(17);
  });

  it("each template has required fields", () => {
    TEMPLATE_CATALOG.forEach((t) => {
      expect(t.name).toBeTruthy();
      expect(t.description).toBeTruthy();
      expect(["data", "chart", "layout", "input"]).toContain(t.category);
      expect(t.propsInterface).toBeTruthy();
      expect(t.example).toBeTruthy();
    });
  });

  it("generates catalog prompt", () => {
    const catalog = generateCatalogPrompt();
    expect(catalog).toContain("Available Components");
    expect(catalog).toContain("import {");
  });
});

describe("buildEnrichedPrompt", () => {
  const msgs = [
    { role: "user", content: "How are the services?" },
    { role: "assistant", content: "All services are healthy." },
  ];

  it("includes triage summary in prompt", () => {
    const prompt = buildEnrichedPrompt(msgs, {}, "Service health status dashboard");
    expect(prompt).toContain("Service health status dashboard");
  });

  it("includes conversation messages", () => {
    const prompt = buildEnrichedPrompt(msgs, {}, "test summary");
    expect(prompt).toContain("User: How are the services?");
    expect(prompt).toContain("Assistant: All services are healthy.");
  });

  it("includes data when provided", () => {
    const data = { health: { status: "ok", services: [{ name: "bridge", status: "healthy" }] } };
    const prompt = buildEnrichedPrompt(msgs, data, "test");
    expect(prompt).toContain("Available Data");
    expect(prompt).toContain("bridge");
    expect(prompt).toContain("healthy");
  });

  it("omits data section when data is empty", () => {
    const prompt = buildEnrichedPrompt(msgs, {}, "test");
    expect(prompt).not.toContain("Available Data");
  });

  it("includes rendering rules with premium helpers", () => {
    const prompt = buildEnrichedPrompt(msgs, {}, "test");
    expect(prompt).toContain("animateValue");
    expect(prompt).toContain("sparklineSVG");
    expect(prompt).toContain("createGradient");
    expect(prompt).toContain("gradient-text");
    expect(prompt).toContain("bento");
    expect(prompt).toContain("glass cards");
  });

  it("limits data to 6000 chars", () => {
    const bigData = { huge: "x".repeat(8000) };
    const prompt = buildEnrichedPrompt(msgs, bigData, "test");
    // The data section should be truncated
    expect(prompt.length).toBeLessThan(10000);
  });

  it("includes up to 4 conversation messages", () => {
    const longMsgs = [
      { role: "user", content: "msg1" },
      { role: "assistant", content: "msg2" },
      { role: "user", content: "msg3" },
      { role: "assistant", content: "msg4" },
      { role: "user", content: "msg5" },
    ];
    const prompt = buildEnrichedPrompt(longMsgs, {}, "test");
    // Should include last 4
    expect(prompt).toContain("msg2");
    expect(prompt).toContain("msg5");
  });
});

describe("Time-Aware Prompts", () => {
  it("morning (8am) includes greeting and premium patterns", () => {
    const prompt = getTimeAwarePrompt(8);
    expect(prompt).toContain("MORNING");
    expect(prompt).toContain("morning");
    expect(prompt).toContain("glass");
    expect(prompt).toContain("gradient-text");
    expect(prompt).toContain("gauge");
  });

  it("midday (13pm) includes status and premium patterns", () => {
    const prompt = getTimeAwarePrompt(13);
    expect(prompt).toContain("MIDDAY");
    expect(prompt).toContain("bento");
    expect(prompt).toContain("Sparklines");
  });

  it("evening (19pm) includes summary and premium patterns", () => {
    const prompt = getTimeAwarePrompt(19);
    expect(prompt).toContain("EVENING");
    expect(prompt).toContain("glass");
    expect(prompt).toContain("doughnut");
  });

  it("night (2am) is minimal with premium touches", () => {
    const prompt = getTimeAwarePrompt(2);
    expect(prompt).toContain("MINIMAL");
    expect(prompt).toContain("glass");
    expect(prompt).toContain("gauge");
  });
});
