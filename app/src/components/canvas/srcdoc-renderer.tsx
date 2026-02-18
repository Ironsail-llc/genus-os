"use client";

import { useMemo, useEffect, useRef, useState } from "react";

interface SrcdocRendererProps {
  html: string;
}

export function SrcdocRenderer({ html }: SrcdocRendererProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(400);

  const srcdoc = useMemo(() => {
    // Basic sanitization — strip script tags with external sources
    const sanitized = html
      .replace(/<script[^>]*src=[^>]*>/gi, "")
      .replace(/<link[^>]*href=["'](?!https:\/\/cdn\.tailwindcss\.com)[^"']*["'][^>]*>/gi, "");

    return `<!DOCTYPE html>
<html class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
    .glass { background: rgba(255,255,255,0.03); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border: 1px solid rgba(255,255,255,0.08); }
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
${sanitized}
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

  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.data?.type === "srcdoc-height" && typeof e.data.height === "number") {
        setHeight(Math.max(200, e.data.height + 32));
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
      title="Dashboard"
      data-testid="srcdoc-renderer"
    />
  );
}
