"""Brand theme — OkLch palette matching the Helm dashboard (app.robothor.ai)."""

from __future__ import annotations

BRAND_VARS = """\
:root {
  --r-bg: oklch(0.12 0.005 285.823);
  --r-surface: oklch(0.16 0.006 285.885);
  --r-surface-hover: oklch(0.20 0.006 285.885);
  --r-border: oklch(1 0 0 / 10%);
  --r-border-strong: oklch(1 0 0 / 15%);
  --r-text: oklch(0.985 0 0);
  --r-text-muted: oklch(0.65 0.015 286.067);
  --r-text-dim: oklch(0.45 0.01 286);
  --r-primary: oklch(0.65 0.2 265);
  --r-primary-dim: oklch(0.55 0.15 265);
  --r-accent: oklch(0.627 0.265 303.9);
  --r-success: oklch(0.696 0.17 162.48);
  --r-warning: oklch(0.769 0.188 70.08);
  --r-danger: oklch(0.645 0.246 16.439);
  --r-radius: 16px;
  --r-radius-sm: 10px;
  --r-radius-lg: 24px;
  --r-font: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, sans-serif;
  --r-font-mono: 'SF Mono', Monaco, 'Cascadia Code', monospace;
}
"""

BRAND_BASE = """\
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  min-height: 100vh;
  background: var(--r-bg);
  color: var(--r-text);
  font-family: var(--r-font);
  overflow-x: hidden;
  line-height: 1.6;
}
.bg-gradient {
  position: fixed; inset: 0; z-index: -1;
  background:
    radial-gradient(ellipse at 20% 20%, oklch(0.65 0.2 265 / 12%) 0%, transparent 50%),
    radial-gradient(ellipse at 80% 80%, oklch(0.627 0.265 303.9 / 10%) 0%, transparent 50%);
  animation: shift 20s ease-in-out infinite;
}
@keyframes shift {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.8; transform: scale(1.05); }
}
.glass {
  background: oklch(1 0 0 / 5%);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  border: 1px solid var(--r-border);
  border-radius: var(--r-radius-lg);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3), inset 0 1px 0 oklch(1 0 0 / 8%);
}
a { color: var(--r-primary); text-decoration: none; }
a:hover { text-decoration: underline; }
code {
  background: oklch(1 0 0 / 8%);
  padding: 0.2rem 0.5rem;
  border-radius: 4px;
  font-family: var(--r-font-mono);
  font-size: 0.9em;
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.85); }
}
"""


def brand_css() -> str:
    """Return combined brand CSS variables and base styles."""
    return BRAND_VARS + BRAND_BASE
