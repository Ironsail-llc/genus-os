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

  // Normalize code
  let normalized = code;

  // Strip markdown fences if the LLM wrapped the code
  normalized = normalized.replace(/^```(?:tsx?|jsx?|html)?\n?/m, "");
  normalized = normalized.replace(/\n?```\s*$/m, "");

  // Attempt to repair double-quoted data-chart attributes
  normalized = normalizeChartQuotes(normalized);

  // Validate chart specs (after normalization)
  const chartErrors = validateChartSpecs(normalized);
  errors.push(...chartErrors);

  return {
    valid: errors.length === 0,
    errors,
    code: normalized.trim(),
  };
}

const VALID_CHART_TYPES = new Set(["bar", "line", "doughnut", "pie", "radar", "polarArea"]);

export function validateChartSpecs(code: string): string[] {
  const errors: string[] = [];
  // Match both single-quoted and double-quoted data-chart attributes
  const singleQuoteRegex = /data-chart='([^']*)'/g;
  const doubleQuoteRegex = /data-chart="([^"]*)"/g;
  let match;
  while ((match = singleQuoteRegex.exec(code)) !== null) {
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
  // Double-quoted data-chart attributes almost always have broken JSON
  // because JSON keys/values use double quotes too (e.g., data-chart="{"type":"bar"}")
  // which truncates the attribute at the first inner double quote.
  // Flag these but don't reject the whole dashboard — the hydration script
  // may still render other charts, and the card gracefully shows "Chart render error".
  while ((match = doubleQuoteRegex.exec(code)) !== null) {
    try {
      // If by chance the JSON is valid (unlikely with double-quote wrapping),
      // validate it normally
      const spec = JSON.parse(match[1]);
      if (!spec.type || !VALID_CHART_TYPES.has(spec.type)) {
        errors.push(`Invalid chart type: ${spec.type}`);
      }
    } catch {
      // Expected — double-quoted wrapping corrupts JSON. This is a warning,
      // not a hard error. We'll attempt repair in normalizeChartQuotes().
    }
  }
  return errors;
}

/**
 * Repair data-chart attributes that use double quotes instead of single quotes.
 * The LLM sometimes generates data-chart="{"type":"bar"...}" which breaks because
 * the HTML attribute ends at the first inner double quote. This attempts to find
 * and repair these by looking for the full JSON object pattern.
 */
export function normalizeChartQuotes(code: string): string {
  // Pattern: data-chart=" followed by what looks like truncated JSON
  // Replace with single-quote wrapping where we can reconstruct the full spec
  return code.replace(
    /data-chart="(\{[^"]*)"([^>]*>)/g,
    (_match, partial, rest) => {
      // The partial is truncated at the first inner " — try to find the closing }
      // by scanning ahead in 'rest' for the pattern }" or }'
      // This is a best-effort heuristic
      const closingMatch = rest.match(/^([^>]*?)'\s*>/);
      if (closingMatch) {
        // Already has single-quote closing — likely a mixed-quote situation
        return `data-chart='${partial}${closingMatch[1]}'${rest.slice(closingMatch[0].length - 1)}`;
      }
      // Can't reliably reconstruct — leave as-is (will show "Chart render error" gracefully)
      return `data-chart="${partial}"${rest}`;
    }
  );
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
