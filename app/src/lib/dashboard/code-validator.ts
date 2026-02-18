/**
 * Basic validation and sanitization of generated dashboard code.
 * Blocks dangerous patterns before rendering.
 */

const BLOCKED_PATTERNS = [
  /\beval\s*\(/,
  /\bFunction\s*\(/,
  /\bnew\s+Function\b/,
  /\bdocument\.cookie\b/,
  /\blocalStorage\b/,
  /\bsessionStorage\b/,
  /\bwindow\.location\b/,
  /\bfetch\s*\(\s*["']https?:\/\/(?!localhost|127\.0\.0\.1)/,
  /\bimport\s*\(\s*["']https?:\/\//,
  /\b__proto__\b/,
  /\bconstructor\s*\[/,
  /\bdocument\.write\b/,
  // innerHTML is safe in our sandboxed iframe (allow-scripts only, no allow-same-origin)
  /\bdangerouslySetInnerHTML\b/,
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
