"use client";

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import DOMPurify from "isomorphic-dompurify";

import { reportDashboardError } from "@/lib/dashboard/error-reporter";

interface SrcdocRendererProps {
  html: string;
  /**
   * Trusted, parent-authored script injected into the srcdoc BEFORE the
   * sanitized model HTML — e.g. CANVAS_SHIM_SOURCE, which defines
   * `window.robothor`. This is OUR code, never the model's, so it is
   * concatenated directly into the srcdoc template (not passed through
   * DOMPurify), exactly like the height/error script below it.
   */
  bootstrap?: string;
  /**
   * `data-testid` for the rendered iframe. Defaults to "srcdoc-renderer" so
   * existing callers (and the isolation test, which renders this component
   * directly) are unaffected. Callers that mount a second SrcdocRenderer
   * alongside another one in the DOM at the same time (e.g. CanvasView,
   * which mounts next to the welcome dashboard's SrcdocRenderer) must pass a
   * distinct id so `[data-testid="srcdoc-renderer"]` locators stay
   * unambiguous.
   */
  testId?: string;
}

/** Render model HTML as a read-only, isolated document with no action channel. */
export const SrcdocRenderer = forwardRef<HTMLIFrameElement, SrcdocRendererProps>(function SrcdocRenderer(
  { html, bootstrap, testId = "srcdoc-renderer" },
  forwardedRef,
) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  useImperativeHandle(forwardedRef, () => iframeRef.current as HTMLIFrameElement, []);
  const [height, setHeight] = useState(400);
  const srcdoc = useMemo(() => {
    const sanitized = DOMPurify.sanitize(html, {
          ADD_TAGS: [
            "svg",
            "polyline",
            "path",
            "circle",
            "rect",
            "line",
            "text",
            "g",
            "defs",
            "linearGradient",
            "stop",
          ],
          ADD_ATTR: [
            "data-testid",
            "viewBox",
            "points",
            "stroke",
            "stroke-width",
            "stroke-linecap",
            "stroke-linejoin",
            "fill",
            "d",
            "cx",
            "cy",
            "r",
            "x1",
            "y1",
            "x2",
            "y2",
            "offset",
            "stop-color",
            "stop-opacity",
            "height",
            "width",
          ],
          ALLOW_DATA_ATTR: false,
          ALLOW_UNKNOWN_PROTOCOLS: false,
          FORBID_TAGS: [
            "script",
            "iframe",
            "object",
            "embed",
            "link",
            "meta",
            "base",
            "a",
            "form",
            "input",
            "button",
            "select",
            "textarea",
            "option",
            "fieldset",
          ],
          FORBID_ATTR: ["srcdoc"],
        });

    return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; font-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none';">
  <style>
    html, body { margin: 0; padding: 0; background: #18181b; color: #fafafa; }
    body { padding: 16px; overflow: hidden; font-family: system-ui, sans-serif; }
    table { border-collapse: collapse; max-width: 100%; }
    img, svg { max-width: 100%; }
  </style>
</head>
<body>
${bootstrap ? `<script>${bootstrap}<\/script>` : ""}
${sanitized}
<script>
  function reportHeight() {
    window.parent.postMessage({ type: 'srcdoc-height', height: document.body.scrollHeight }, '*');
  }
  if (document.readyState === 'complete') reportHeight();
  else window.addEventListener('load', reportHeight);
  new ResizeObserver(reportHeight).observe(document.body);
  window.onerror = function(message, source, line, column) {
    window.parent.postMessage({
      type: 'robothor:error',
      source: 'read-only-dashboard',
      message: String(message).slice(0, 500),
      details: { line: line, column: column }
    }, '*');
  };
<\/script>
</body>
</html>`;
  }, [html, bootstrap]);

  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const source = iframeRef.current?.contentWindow;
      if (!source || event.source !== source || event.origin !== "null") return;

      if (
        event.data?.type === "srcdoc-height" &&
        typeof event.data.height === "number" &&
        Number.isFinite(event.data.height)
      ) {
        setHeight(Math.max(200, Math.min(event.data.height + 32, 5000)));
        return;
      }
      if (event.data?.type === "robothor:error") {
        const message = String(event.data.message ?? "dashboard error").slice(0, 500);
        console.error(`[iframe-error] read-only-dashboard: ${message}`);
        reportDashboardError("iframe/read-only-dashboard", message);
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  return (
    <iframe
      ref={iframeRef}
      srcDoc={srcdoc}
      className="w-full border-0"
      style={{ height: `${height}px` }}
      sandbox="allow-scripts"
      title="Read-only generated dashboard"
      data-testid={testId}
      referrerPolicy="no-referrer"
    />
  );
});
