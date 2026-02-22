/**
 * Tests for code-validator Phase 4 changes.
 * onclick and onsubmit are now allowed for robothor.action() calls.
 * Other inline event handlers remain blocked.
 */

import { describe, it, expect } from "vitest";
import { validateDashboardCode } from "@/lib/dashboard/code-validator";

describe("validateDashboardCode — interactive handler rules", () => {
  it("allows onclick for robothor.action()", () => {
    const code = `<button onclick="robothor.action('crm_health', {})">Health Check</button>`;
    expect(validateDashboardCode(code).valid).toBe(true);
  });

  it("allows onsubmit for robothor.submit()", () => {
    const code = `<form onsubmit="event.preventDefault(); robothor.submit('create_note', '#note-form')">
      <input name="title"><button type="submit">Save</button>
    </form>`;
    expect(validateDashboardCode(code).valid).toBe(true);
  });

  it("allows onclick with double quotes", () => {
    const code = `<button onclick="robothor.action('list_people', {limit: 5})">List</button>`;
    expect(validateDashboardCode(code).valid).toBe(true);
  });

  it("allows onclick with single quotes", () => {
    const code = `<button onclick='robothor.action("crm_health", {})'>Check</button>`;
    expect(validateDashboardCode(code).valid).toBe(true);
  });

  it("still blocks onerror", () => {
    const code = `<img onerror="alert(1)" src="x">`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks onload", () => {
    const code = `<body onload="steal()">test</body>`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks onmouseover", () => {
    const code = `<div onmouseover="alert(document.cookie)">hover</div>`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks onfocus", () => {
    const code = `<input onfocus="alert(1)">`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks onblur", () => {
    const code = `<input onblur="alert(1)">`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks onchange", () => {
    const code = `<select onchange="evil()"><option>A</option></select>`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks onkeyup", () => {
    const code = `<input onkeyup="steal(this.value)">`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("still blocks ondrag", () => {
    const code = `<div ondrag="evil()">drag me</div>`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });
});
