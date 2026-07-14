import { describe, expect, it } from "vitest";

import { validateDashboardCode } from "@/lib/dashboard/code-validator";

describe("validateDashboardCode executable-capability denial", () => {
  it.each([
    `<button onclick="robothor.action('crm_health', {})">Run</button>`,
    `<form onsubmit="postMessage({type:'robothor:action'}, '*')"></form>`,
    `<script>fetch('/api/actions/execute')</script>`,
    `<a href="javascript:alert(1)">bad</a>`,
    `<link rel="stylesheet" href="https://evil.test/x.css">`,
    `<style>@import 'https://evil.test/x.css';</style>`,
    `<style>.x{background:url(https://evil.test/x)}</style>`,
    `<a href="/internal/read">static link</a>`,
    `<form><input value="x"></form>`,
    `<button type="button">static button</button>`,
    `<select><option>one</option></select>`,
    `<textarea>notes</textarea>`,
    `<fieldset><p>controls</p></fieldset>`,
  ])("rejects executable or network-bearing markup", (code) => {
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("accepts static semantic HTML and local CSS", () => {
    const code = `<section class="genus-dashboard"><style>.metric{color:#fff}</style><article><h2>Health</h2><p>3 healthy</p></article></section>`;
    expect(validateDashboardCode(code).valid).toBe(true);
  });
});
