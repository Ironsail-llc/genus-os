import { describe, it, expect } from "vitest";
import {
  getComponent,
  getAllTools,
  hasComponent,
} from "@/lib/component-registry";

describe("Component Registry", () => {
  it("resolves component for known tool name", () => {
    const tool = getComponent("render_contact_card");
    expect(tool).toBeDefined();
    expect(tool!.component).toBeDefined();
  });

  it("returns undefined for unknown tool name", () => {
    const tool = getComponent("render_nonexistent");
    expect(tool).toBeUndefined();
  });

  it("hasComponent returns true for registered tools", () => {
    expect(hasComponent("render_contact_table")).toBe(true);
    expect(hasComponent("render_fake")).toBe(false);
  });

  it("getAllTools returns complete tool list", () => {
    const tools = getAllTools();
    expect(tools.length).toBeGreaterThanOrEqual(15);

    const names = tools.map((t) => t.name);
    expect(names).toContain("render_contact_card");
    expect(names).toContain("render_contact_table");
    expect(names).toContain("render_conversations");
    expect(names).toContain("render_bar_chart");
    expect(names).toContain("render_memory_search");
    expect(names).toContain("render_service_health");
    expect(names).toContain("render_task_board");
    expect(names).toContain("render_markdown");
  });

  it("each tool has name, description, and component", () => {
    const tools = getAllTools();
    tools.forEach((tool) => {
      expect(tool.name).toBeTruthy();
      expect(tool.description).toBeTruthy();
      expect(tool.component).toBeDefined();
    });
  });
});
