/**
 * What the main agent is told about workspaces (H1 and the workspace rules),
 * alongside the canvas markers it already knows. Asserted as properties of the
 * prompt, not its exact wording.
 */
import { describe, expect, it } from "vitest";

import { getVisualCanvasPrompt } from "../system-prompt";

const prompt = getVisualCanvasPrompt();

describe("getVisualCanvasPrompt", () => {
  it("keeps the canvas markers it has always described", () => {
    expect(prompt).toContain("[DASHBOARD:");
    expect(prompt).toContain("[RENDER:");
  });

  it("tells the agent to link workspaces with markdown links to the room route", () => {
    expect(prompt).toMatch(/workspace/i);
    expect(prompt).toContain("/?v=chat&room=");
    expect(prompt).toMatch(/markdown link/i);
  });

  it("says markers are stripped on reload, so a workspace is never linked by a marker", () => {
    expect(prompt).toMatch(/never .*marker/i);
    expect(prompt).toMatch(/reload/i);
  });

  it("names room_publish_edition for report visuals inside a workspace", () => {
    expect(prompt).toContain("room_publish_edition");
  });

  it("asks the agent to restate and confirm before creating a workspace", () => {
    expect(prompt).toMatch(/restate/i);
    expect(prompt).toMatch(/confirm/i);
    expect(prompt).toContain("room_create");
  });

  it("forbids describing a workspace the tools did not return", () => {
    expect(prompt).toMatch(/never describe a workspace/i);
    expect(prompt).toContain("room_list");
  });

  it("keeps the user-facing name: workspaces, not rooms", () => {
    const prose = prompt.replace(/room_[a-z_]+/g, "").replace(/[?&]room=/g, "");
    expect(prose).not.toMatch(/\broom\b/i);
  });
});
