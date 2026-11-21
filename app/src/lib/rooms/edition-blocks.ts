import type { EditionBlock } from "./types";

/**
 * The components an edition may use (contract → Edition blocks). A subset of
 * the dashboard's component registry, kept so by a test. Anything else in an
 * edition is drawn as an "unsupported block" placeholder, never rendered.
 */
export const EDITION_COMPONENTS = [
  "render_metric_grid",
  "render_metric_card",
  "render_bar_chart",
  "render_line_chart",
  "render_pie_chart",
  "render_data_table",
  "render_markdown",
  "render_timeline",
] as const;

const ALLOWED = new Set<string>(EDITION_COMPONENTS);

export function isRenderableBlock(block: unknown): block is EditionBlock {
  if (!block || typeof block !== "object") return false;
  const { component, props } = block as Record<string, unknown>;
  return (
    typeof component === "string" &&
    ALLOWED.has(component) &&
    !!props &&
    typeof props === "object" &&
    !Array.isArray(props)
  );
}
