/**
 * Dashboard web vitals: the operator's switch and the shape a reported metric
 * must have before it is stored or logged.
 *
 * Shared by the root layout (which mounts the reporter only when vitals are on)
 * and POST /api/vitals (which validates every beacon). Nothing here identifies
 * a tenant or a person: a metric is its name, value, rating, the page's path
 * without its query string, and the dashboard view.
 */

const OFF = new Set(["0", "false", "off", "no"]);

/** On unless the operator set GENUS_DASHBOARD_WEB_VITALS to a false value. */
export function webVitalsEnabled(
  env: Record<string, string | undefined> = {
    GENUS_DASHBOARD_WEB_VITALS: process.env.GENUS_DASHBOARD_WEB_VITALS,
  }
): boolean {
  const raw = (env.GENUS_DASHBOARD_WEB_VITALS ?? "").trim().toLowerCase();
  return !OFF.has(raw);
}

export const METRIC_NAMES = ["CLS", "FCP", "FID", "INP", "LCP", "TTFB"] as const;
export const RATINGS = ["good", "needs-improvement", "poor"] as const;
const NAVIGATION_TYPES = new Set([
  "navigate",
  "reload",
  "back-forward",
  "back-forward-cache",
  "prerender",
  "restore",
]);

/** An hour, in milliseconds: anything larger is not a page-load measurement. */
const MAX_VALUE = 3_600_000;
const VIEW_RE = /^[a-z][a-z0-9-]{0,31}$/;
const ID_RE = /^[\w.-]{1,64}$/;

export interface Vital {
  name: (typeof METRIC_NAMES)[number];
  value: number;
  rating: (typeof RATINGS)[number];
  path: string;
  view?: string;
  id?: string;
  navigation_type?: string;
}

/**
 * The validated metric, or null when the body is not one. Unknown fields are
 * dropped rather than refused, so nothing a client adds is ever recorded; the
 * path keeps only its pathname.
 */
export function parseVital(body: unknown): Vital | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;

  if (!(METRIC_NAMES as readonly unknown[]).includes(b.name)) return null;
  if (typeof b.value !== "number" || !Number.isFinite(b.value)) return null;
  if (b.value < 0 || b.value > MAX_VALUE) return null;
  if (!(RATINGS as readonly unknown[]).includes(b.rating)) return null;
  if (typeof b.path !== "string" || !b.path.startsWith("/") || b.path.startsWith("//")) {
    return null;
  }
  const path = b.path.split(/[?#]/, 1)[0].slice(0, 256);

  const vital: Vital = {
    name: b.name as Vital["name"],
    value: b.value,
    rating: b.rating as Vital["rating"],
    path,
  };
  if (b.view !== undefined) {
    if (typeof b.view !== "string" || !VIEW_RE.test(b.view)) return null;
    vital.view = b.view;
  }
  if (typeof b.id === "string" && ID_RE.test(b.id)) vital.id = b.id;
  if (typeof b.navigation_type === "string" && NAVIGATION_TYPES.has(b.navigation_type)) {
    vital.navigation_type = b.navigation_type;
  }
  return vital;
}
