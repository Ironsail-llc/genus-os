"use client";

import { useMemo, useEffect, useRef, useState, useCallback } from "react";
import DOMPurify from "dompurify";

/**
 * Tools that dashboard actions can invoke.
 * Must match tools available to the helm-user agent in agent_capabilities.json.
 */
export const ACTION_ALLOWLIST = new Set([
  // CRM read
  "list_conversations",
  "get_conversation",
  "list_messages",
  "list_people",
  "crm_health",
  // CRM write (limited)
  "create_note",
  "create_message",
  "toggle_conversation_status",
  "log_interaction",
]);

interface SrcdocRendererProps {
  html: string;
  onAction?: (action: { tool: string; params: Record<string, unknown>; id: string }) => void;
}

export function SrcdocRenderer({ html, onAction }: SrcdocRendererProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(400);

  const srcdoc = useMemo(() => {
    const sanitized = DOMPurify.sanitize(html, {
      ADD_TAGS: ["canvas", "svg", "polyline", "path", "circle", "rect", "line", "text", "g", "defs", "linearGradient", "stop", "form", "textarea", "select", "input"],
      ADD_ATTR: ["data-chart", "data-testid", "data-tab", "data-sort-dir", "viewBox", "points", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "fill", "d", "cx", "cy", "r", "x1", "y1", "x2", "y2", "offset", "stop-color", "stop-opacity", "height", "width", "onclick", "onsubmit", "placeholder", "rows", "required", "disabled"],
      ALLOW_DATA_ATTR: true,
      ALLOW_UNKNOWN_PROTOCOLS: false,
      FORBID_TAGS: ["iframe", "object", "embed", "meta"],
      FORBID_ATTR: ["onerror", "onload", "onmouseover", "onfocus", "onblur"],
    });

    return `<!DOCTYPE html>
<html class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; style-src 'unsafe-inline' https://cdn.tailwindcss.com; img-src data: blob:; font-src https://cdn.tailwindcss.com; connect-src 'none';">
  <script src="https://cdn.tailwindcss.com"><\/script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"><\/script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2"><\/script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3"><\/script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: { extend: {} }
    };
    // Register plugins
    Chart.register(ChartDataLabels);
    // Chart.js global defaults for dark theme
    Chart.defaults.color = '#a1a1aa';
    Chart.defaults.borderColor = '#27272a';
    Chart.defaults.backgroundColor = 'rgba(99, 102, 241, 0.5)';
    Chart.defaults.font.family = 'system-ui, -apple-system, sans-serif';
    Chart.defaults.animation.duration = 750;
    Chart.defaults.animation.easing = 'easeOutQuart';
    Chart.defaults.elements.bar.borderRadius = 6;
    Chart.defaults.elements.bar.borderSkipped = false;
    Chart.defaults.elements.line.tension = 0.4;
    Chart.defaults.elements.point.radius = 3;
    Chart.defaults.elements.point.hoverRadius = 6;
    Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(0,0,0,0.8)';
    Chart.defaults.plugins.tooltip.cornerRadius = 8;
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.titleFont = { weight: '600' };
    Chart.defaults.plugins.datalabels.display = false;
  <\/script>
  <style>
    body { background: #18181b; color: #fafafa; font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 16px; overflow: hidden; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    .animate-in { animation: fadeIn 0.4s ease-out both; }
    .animate-in:nth-child(1) { animation-delay: 0s; }
    .animate-in:nth-child(2) { animation-delay: 0.05s; }
    .animate-in:nth-child(3) { animation-delay: 0.1s; }
    .animate-in:nth-child(4) { animation-delay: 0.15s; }
    .animate-in:nth-child(5) { animation-delay: 0.2s; }
    .animate-in:nth-child(6) { animation-delay: 0.25s; }
    .animate-in:nth-child(7) { animation-delay: 0.3s; }
    .animate-in:nth-child(8) { animation-delay: 0.35s; }
    .animate-in:nth-child(9) { animation-delay: 0.4s; }
    .animate-in:nth-child(10) { animation-delay: 0.45s; }
    .animate-in:nth-child(11) { animation-delay: 0.5s; }
    .animate-in:nth-child(12) { animation-delay: 0.55s; }
    .glass { background: rgba(255,255,255,0.03); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255,255,255,0.08); overflow: hidden; }
    [class*="bg-zinc-900"] { overflow: hidden; }
    [class*="col-span"] { overflow: hidden; }
    [data-chart] { width: 100%; max-height: 100%; }
    canvas { max-width: 100%; }
    .gradient-text { background: linear-gradient(135deg, #818cf8, #a78bfa, #c084fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
    body::before { content: ''; position: fixed; inset: 0; background: radial-gradient(ellipse at 20% 50%, rgba(99,102,241,0.08), transparent 50%), radial-gradient(ellipse at 80% 20%, rgba(168,85,247,0.06), transparent 50%); pointer-events: none; z-index: 0; }
    body > * { position: relative; z-index: 1; }
    @property --num { syntax: '<integer>'; initial-value: 0; inherits: false; }
    .counter { transition: --num 1.5s ease-out; counter-reset: num var(--num); }
    .counter::after { content: counter(num); }
    @keyframes pulse-glow { 0%,100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.4); } 50% { box-shadow: 0 0 8px 2px rgba(34,197,94,0.2); } }
    .pulse-live { animation: pulse-glow 2s ease-in-out infinite; }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #3f3f46; border-radius: 3px; }
  </style>
</head>
<body>
<script>
  // Gradient fill factory for Chart.js
  function createGradient(ctx, colorStops) {
    var g = ctx.createLinearGradient(0, 0, 0, ctx.canvas.height);
    colorStops.forEach(function(s, i) { g.addColorStop(i / (colorStops.length - 1), s); });
    return g;
  }
  // Animated number counter
  function animateValue(el, start, end, duration) {
    var startTime = null;
    function step(timestamp) {
      if (!startTime) startTime = timestamp;
      var progress = Math.min((timestamp - startTime) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = Math.floor(start + (end - start) * eased).toLocaleString();
      if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  // Inline SVG sparkline
  function sparklineSVG(data, color, w, h) {
    if (!data || !data.length) return '';
    w = w || 80; h = h || 24;
    var max = Math.max.apply(null, data), min = Math.min.apply(null, data);
    var range = max - min || 1;
    var points = data.map(function(v, i) {
      return (i * w / (data.length - 1)).toFixed(1) + ',' + (h - ((v - min) / range) * h).toFixed(1);
    }).join(' ');
    return '<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'">'
      + '<polyline points="'+points+'" fill="none" stroke="'+color+'" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><\/svg>';
  }
<\/script>
<script>
  // Chart hydration — renders declarative chart specs from data-chart attributes
  (function() {
    var COLORS = {
      indigo: '#6366f1', purple: '#8b5cf6', emerald: '#22c55e',
      rose: '#ef4444', yellow: '#eab308', blue: '#3b82f6',
      cyan: '#06b6d4', orange: '#f97316', pink: '#ec4899', zinc: '#a1a1aa'
    };

    function resolveColor(name) { return COLORS[name] || name; }

    function hexToRgb(hex) {
      var r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
      return r+','+g+','+b;
    }

    function hydrateCharts() {
      document.querySelectorAll('[data-chart]').forEach(function(el) {
        try {
          var spec = JSON.parse(el.getAttribute('data-chart'));
          var canvas = document.createElement('canvas');
          // Use explicit height, or fit to parent container, or 200px fallback
          var parentH = el.parentElement ? el.parentElement.clientHeight : 0;
          var availH = parentH > 60 ? parentH - 40 : 0; // subtract heading/padding estimate
          canvas.height = spec.height || (availH > 80 ? Math.min(availH, 300) : 200);
          el.appendChild(canvas);
          var ctx = canvas.getContext('2d');

          var datasets = (spec.datasets || []).map(function(ds) {
            var color = resolveColor(ds.color || 'indigo');
            return {
              label: ds.label || '',
              data: ds.data,
              backgroundColor: spec.gradient
                ? createGradient(ctx, ['rgba(' + hexToRgb(color) + ',0.8)', 'rgba(' + hexToRgb(color) + ',0.1)'])
                : ds.colors ? ds.colors.map(resolveColor) : color,
              borderColor: color,
              borderWidth: ds.borderWidth !== undefined ? ds.borderWidth : (spec.type === 'line' ? 2 : 0),
              fill: spec.type === 'line' ? true : undefined,
              pointBackgroundColor: spec.type === 'line' ? color : undefined
            };
          });

          new Chart(ctx, {
            type: spec.type,
            data: { labels: spec.labels, datasets: datasets },
            options: {
              responsive: true,
              indexAxis: spec.indexAxis || 'x',
              cutout: spec.cutout,
              rotation: spec.rotation,
              circumference: spec.circumference,
              plugins: {
                legend: { display: spec.legend !== false && datasets.length > 1 },
                datalabels: { display: !!spec.datalabels, color: '#a1a1aa', anchor: 'end', align: 'top', font: { size: 11 } },
                tooltip: spec.type === 'doughnut' && spec.cutout ? { enabled: false } : undefined
              },
              scales: ['doughnut','pie','radar','polarArea'].includes(spec.type) ? undefined : {
                y: { grid: { color: '#27272a' } },
                x: { grid: { display: false } }
              }
            }
          });
        } catch(e) {
          el.innerHTML = '<p class="text-xs text-rose-400/60">Chart render error</p>';
        }
      });
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', hydrateCharts);
    } else {
      hydrateCharts();
    }
  })();
<\/script>
${sanitized}
<script>
  // ─── Robothor Action API ──────────────────────────────────────
  // Provides robothor.action() and robothor.submit() for dashboard interactivity.
  // Actions are sent to the parent via postMessage and executed via Bridge API.
  window.robothor = {
    _actionId: 0,
    _callbacks: {},
    action: function(tool, params) {
      var id = 'action-' + (++this._actionId) + '-' + Date.now();
      var self = this;
      return new Promise(function(resolve, reject) {
        self._callbacks[id] = { resolve: resolve, reject: reject };
        window.parent.postMessage({
          type: 'robothor:action',
          tool: tool,
          params: params || {},
          id: id
        }, '*');
        // Timeout after 30s
        setTimeout(function() {
          if (self._callbacks[id]) {
            self._callbacks[id].reject(new Error('Action timed out'));
            delete self._callbacks[id];
          }
        }, 30000);
      });
    },
    submit: function(tool, formSelector) {
      var form = document.querySelector(formSelector);
      if (!form) return Promise.reject(new Error('Form not found: ' + formSelector));
      var formData = new FormData(form);
      var params = {};
      formData.forEach(function(value, key) { params[key] = value; });
      return this.action(tool, params);
    },
    _handleResult: function(id, success, data, error) {
      var cb = this._callbacks[id];
      if (cb) {
        if (success) cb.resolve(data);
        else cb.reject(new Error(error || 'Action failed'));
        delete this._callbacks[id];
      }
    }
  };
  // Listen for action results from parent
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'robothor:action-result') {
      window.robothor._handleResult(e.data.id, e.data.success, e.data.data, e.data.error);
    }
  });
<\/script>
<script>
  function reportHeight() {
    var h = document.body.scrollHeight;
    window.parent.postMessage({ type: 'srcdoc-height', height: h }, '*');
  }
  // Report after Tailwind renders
  if (document.readyState === 'complete') reportHeight();
  else window.addEventListener('load', function() { setTimeout(reportHeight, 100); });
  // Report after Tailwind JIT + Chart.js rendering
  setTimeout(reportHeight, 500);
  setTimeout(reportHeight, 1500);
<\/script>
</body>
</html>`;
  }, [html]);

  // Send action result back to iframe
  const sendActionResult = useCallback(
    (id: string, success: boolean, data?: unknown, error?: string) => {
      iframeRef.current?.contentWindow?.postMessage(
        { type: "robothor:action-result", id, success, data, error },
        "*"
      );
    },
    []
  );

  useEffect(() => {
    function onMessage(e: MessageEvent) {
      // srcdoc iframes have origin "null" (string), so accept that
      if (e.origin !== "null" && e.origin !== window.location.origin) return;
      if (e.data?.type === "srcdoc-height" && typeof e.data.height === "number") {
        setHeight(Math.max(200, Math.min(e.data.height + 32, 5000)));
      }
      // Handle action requests from dashboard
      if (e.data?.type === "robothor:action" && typeof e.data.tool === "string") {
        const { tool, params, id } = e.data;
        if (!ACTION_ALLOWLIST.has(tool)) {
          sendActionResult(id, false, undefined, `Tool '${tool}' not in action allowlist`);
          return;
        }
        if (onAction) {
          onAction({ tool, params: params || {}, id });
        } else {
          sendActionResult(id, false, undefined, "No action handler configured");
        }
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [onAction, sendActionResult]);

  return (
    <iframe
      ref={iframeRef}
      srcDoc={srcdoc}
      className="w-full border-0"
      style={{ height: `${height}px` }}
      sandbox="allow-scripts"
      title="Dashboard"
      data-testid="srcdoc-renderer"
      referrerPolicy="no-referrer"
    />
  );
}
