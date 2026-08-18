/**
 * Design tokens injected into the sandboxed canvas iframe as CSS variables,
 * so model-generated HTML can match the app shell in both themes.
 *
 * Values mirror src/app/globals.css (the source of truth) — update both
 * together when the palette changes. The iframe cannot inherit the parent's
 * stylesheet (isolated document, strict CSP), so the palette is duplicated
 * here as plain CSS text.
 */

const SHARED_VARS = `--radius: 10px;`;

export const CANVAS_VARS_LIGHT = `
  ${SHARED_VARS}
  --background: oklch(0.972 0.003 280);
  --foreground: oklch(0.22 0.012 280);
  --card: oklch(0.995 0.001 280);
  --border: oklch(0 0 0 / 9%);
  --muted: oklch(0.945 0.004 280);
  --muted-foreground: oklch(0.52 0.012 280);
  --primary: oklch(0.5 0.17 295);
  --brand-2: oklch(0.56 0.12 185);
  --success: oklch(0.58 0.13 160);
  --warning: oklch(0.62 0.13 80);
  --destructive: oklch(0.56 0.17 25);
  --info: oklch(0.56 0.12 230);
`;

export const CANVAS_VARS_DARK = `
  ${SHARED_VARS}
  --background: oklch(0.16 0.009 280);
  --foreground: oklch(0.94 0.004 280);
  --card: oklch(0.2 0.01 280);
  --border: oklch(1 0 0 / 9%);
  --muted: oklch(0.24 0.011 280);
  --muted-foreground: oklch(0.67 0.012 280);
  --primary: oklch(0.66 0.17 290);
  --brand-2: oklch(0.76 0.13 180);
  --success: oklch(0.74 0.13 160);
  --warning: oklch(0.8 0.13 80);
  --destructive: oklch(0.7 0.15 25);
  --info: oklch(0.74 0.11 230);
`;

/** Full iframe stylesheet; theme selected by a `dark` class on <html>. */
export const CANVAS_BASE_STYLES = `
    :root { ${CANVAS_VARS_LIGHT} }
    html.dark { ${CANVAS_VARS_DARK} }
    html, body { margin: 0; padding: 0; background: var(--background); color: var(--foreground); }
    body { padding: 16px; overflow: hidden; font-family: system-ui, sans-serif; }
    table { border-collapse: collapse; max-width: 100%; }
    img, svg { max-width: 100%; }
`;
