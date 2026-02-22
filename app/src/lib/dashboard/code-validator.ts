/**
 * Basic validation and sanitization of generated dashboard code.
 * Blocks dangerous patterns before rendering.
 */

const BLOCKED_PATTERNS = [
  /\beval\s*\(/i,
  /\bFunction\s*\(/,
  /\bnew\s+Function\b/,
  /\bdocument\.cookie\b/i,
  /\blocalStorage\b/i,
  /\bsessionStorage\b/i,
  /\bwindow\.location\b/i,
  /\bfetch\s*\(\s*["']https?:\/\/(?!localhost|127\.0\.0\.1)/i,
  /\bimport\s*\(\s*["']https?:\/\//i,
  /\b__proto__\b/,
  /\bconstructor\s*\[/,
  /\bdocument\.write\b/i,
  /\bdangerouslySetInnerHTML\b/,
  /\bsetTimeout\s*\(\s*["'`]/i,
  /\bsetInterval\s*\(\s*["'`]/i,
  // Allow onclick/onsubmit for robothor.action() calls, block other inline handlers
  /\bon(?!click|submit)\w+\s*=\s*["']/i,
  /\bwindow\s*\[\s*["']/,
  /\(\s*0\s*,\s*eval\s*\)/,
  /\bXMLHttpRequest\b/i,
];

export interface ValidationResult {
  valid: boolean;
  errors: string[];
  code: string;
}

export function validateDashboardCode(code: string): ValidationResult {
  const errors: string[] = [];

  if (!code || code.trim().length === 0) {
    return { valid: false, errors: ["Empty code"], code };
  }

  // Check for blocked patterns
  for (const pattern of BLOCKED_PATTERNS) {
    if (pattern.test(code)) {
      errors.push(`Blocked pattern: ${pattern.source}`);
    }
  }

  // Validate chart specs
  const chartErrors = validateChartSpecs(code);
  errors.push(...chartErrors);

  // Normalize code
  let normalized = code;

  // Strip markdown fences if the LLM wrapped the code
  normalized = normalized.replace(/^```(?:tsx?|jsx?|html)?\n?/m, "");
  normalized = normalized.replace(/\n?```\s*$/m, "");

  return {
    valid: errors.length === 0,
    errors,
    code: normalized.trim(),
  };
}

const VALID_CHART_TYPES = new Set(["bar", "line", "doughnut", "pie", "radar", "polarArea"]);

export function validateChartSpecs(code: string): string[] {
  const errors: string[] = [];
  const specRegex = /data-chart='([^']*)'/g;
  let match;
  while ((match = specRegex.exec(code)) !== null) {
    try {
      const spec = JSON.parse(match[1]);
      if (!spec.type || !VALID_CHART_TYPES.has(spec.type)) {
        errors.push(`Invalid chart type: ${spec.type}`);
      }
      if (spec.datasets && !Array.isArray(spec.datasets)) {
        errors.push("Chart datasets must be an array");
      }
    } catch {
      errors.push("Invalid JSON in data-chart attribute");
    }
  }
  return errors;
}

/** Detect whether generated code is TSX or HTML */
export function detectCodeType(code: string): "tsx" | "html" {
  if (
    code.includes("export default") ||
    code.includes("import ") ||
    code.includes("function Dashboard") ||
    code.includes("const Dashboard") ||
    code.includes("useState")
  ) {
    return "tsx";
  }
  return "html";
}
