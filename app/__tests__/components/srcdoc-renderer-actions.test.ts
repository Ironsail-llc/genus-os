/**
 * Tests for SrcdocRenderer action protocol (Phase 4).
 * Validates: allowlist enforcement, srcdoc content, DOMPurify config.
 */

import { describe, it, expect } from "vitest";
import { render, screen, act } from "@testing-library/react";
import React from "react";
import { ACTION_ALLOWLIST, SrcdocRenderer } from "@/components/canvas/srcdoc-renderer";

async function getSrcdoc(html: string): Promise<string> {
  render(React.createElement(SrcdocRenderer, { html }));
  // Wait for async DOMPurify import to complete and re-render
  await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
  const iframe = screen.getByTestId("srcdoc-renderer") as HTMLIFrameElement;
  return iframe.getAttribute("srcdoc") || "";
}

describe("ACTION_ALLOWLIST", () => {
  it("contains expected CRM read tools", () => {
    expect(ACTION_ALLOWLIST.has("list_conversations")).toBe(true);
    expect(ACTION_ALLOWLIST.has("get_conversation")).toBe(true);
    expect(ACTION_ALLOWLIST.has("list_messages")).toBe(true);
    expect(ACTION_ALLOWLIST.has("list_people")).toBe(true);
    expect(ACTION_ALLOWLIST.has("crm_health")).toBe(true);
  });

  it("contains expected CRM write tools", () => {
    expect(ACTION_ALLOWLIST.has("create_note")).toBe(true);
    expect(ACTION_ALLOWLIST.has("create_message")).toBe(true);
    expect(ACTION_ALLOWLIST.has("toggle_conversation_status")).toBe(true);
    expect(ACTION_ALLOWLIST.has("log_interaction")).toBe(true);
  });

  it("does NOT contain dangerous tools", () => {
    expect(ACTION_ALLOWLIST.has("delete_person")).toBe(false);
    expect(ACTION_ALLOWLIST.has("delete_company")).toBe(false);
    expect(ACTION_ALLOWLIST.has("merge_contacts")).toBe(false);
    expect(ACTION_ALLOWLIST.has("merge_companies")).toBe(false);
    expect(ACTION_ALLOWLIST.has("update_person")).toBe(false);
    expect(ACTION_ALLOWLIST.has("update_company")).toBe(false);
  });

  it("contains task coordination tools", () => {
    expect(ACTION_ALLOWLIST.has("list_tasks")).toBe(true);
    expect(ACTION_ALLOWLIST.has("update_task")).toBe(true);
    expect(ACTION_ALLOWLIST.has("resolve_task")).toBe(true);
    expect(ACTION_ALLOWLIST.has("get_task_history")).toBe(true);
    expect(ACTION_ALLOWLIST.has("agent_status")).toBe(true);
  });

  it("contains routine management tools", () => {
    expect(ACTION_ALLOWLIST.has("list_routines")).toBe(true);
    expect(ACTION_ALLOWLIST.has("create_routine")).toBe(true);
    expect(ACTION_ALLOWLIST.has("update_routine")).toBe(true);
    expect(ACTION_ALLOWLIST.has("delete_routine")).toBe(true);
  });

  it("has exactly 18 tools", () => {
    expect(ACTION_ALLOWLIST.size).toBe(18);
  });
});

describe("SrcdocRenderer — action protocol in srcdoc", () => {
  it("includes robothor action API script", async () => {
    const srcdoc = await getSrcdoc("<div>Test</div>");
    expect(srcdoc).toContain("window.robothor");
    expect(srcdoc).toContain("robothor:action");
    expect(srcdoc).toContain("robothor:action-result");
    expect(srcdoc).toContain("_handleResult");
  });

  it("includes action() and submit() functions", async () => {
    const srcdoc = await getSrcdoc("<div>Test</div>");
    expect(srcdoc).toContain("action: function(tool, params)");
    expect(srcdoc).toContain("submit: function(tool, formSelector)");
  });

  it("action() posts message to parent", async () => {
    const srcdoc = await getSrcdoc("<div>Test</div>");
    expect(srcdoc).toContain("window.parent.postMessage");
    expect(srcdoc).toContain("type: 'robothor:action'");
  });

  it("action result listener forwards to _handleResult", async () => {
    const srcdoc = await getSrcdoc("<div>Test</div>");
    expect(srcdoc).toContain("robothor:action-result");
    expect(srcdoc).toContain("window.robothor._handleResult");
  });
});

describe("SrcdocRenderer — DOMPurify config for interactivity", () => {
  it("preserves form elements", async () => {
    const html = '<form id="test"><input name="title" placeholder="Title"><textarea name="body" rows="3"></textarea><select name="type"><option>A</option></select></form>';
    const srcdoc = await getSrcdoc(html);

    expect(srcdoc).toContain("<form");
    expect(srcdoc).toContain("<input");
    expect(srcdoc).toContain("<textarea");
    expect(srcdoc).toContain("<select");
  });

  it("preserves onclick attribute", async () => {
    const html = `<button onclick="robothor.action('crm_health', {})">Check</button>`;
    const srcdoc = await getSrcdoc(html);
    expect(srcdoc).toContain("onclick");
  });

  it("preserves onsubmit attribute", async () => {
    const html = `<form onsubmit="event.preventDefault(); robothor.submit('create_note', '#f')"><button type="submit">Save</button></form>`;
    const srcdoc = await getSrcdoc(html);
    expect(srcdoc).toContain("onsubmit");
  });

  it("strips onerror attribute", async () => {
    const html = '<img onerror="alert(1)" src="x">';
    const srcdoc = await getSrcdoc(html);
    // The user-provided HTML section should NOT have onerror
    // But the srcdoc template has its own scripts, so we check the sanitized portion
    // DOMPurify removes onerror from user HTML
    expect(srcdoc).not.toMatch(/onerror="alert/);
  });

  it("strips onload attribute from user HTML", async () => {
    const html = '<div onload="steal()">test</div>';
    const srcdoc = await getSrcdoc(html);
    expect(srcdoc).not.toMatch(/onload="steal/);
  });

  it("strips onmouseover attribute", async () => {
    const html = '<div onmouseover="hack()">hover</div>';
    const srcdoc = await getSrcdoc(html);
    expect(srcdoc).not.toMatch(/onmouseover="hack/);
  });

  it("preserves placeholder and required attributes on inputs", async () => {
    const html = '<input placeholder="Enter name" required>';
    const srcdoc = await getSrcdoc(html);
    expect(srcdoc).toContain('placeholder="Enter name"');
    expect(srcdoc).toContain("required");
  });
});
