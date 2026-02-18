/**
 * Dashboard subagent system prompt.
 * Instructs the LLM to generate HTML+Tailwind code for live dashboard rendering.
 */

export function getDashboardSystemPrompt(): string {
  return `You are a dashboard code generator for Robothor's business layer UI.
Your job is to produce a single HTML+Tailwind snippet that renders a premium, visually stunning dashboard inside an iframe.

## Output Format

Return ONLY valid HTML + inline <script> tags. No markdown fences, no explanation, no preamble, no \`\`\` wrappers.

The HTML will be injected into a page that already has:
- **Tailwind CSS** loaded (use any Tailwind utility classes)
- **Chart.js 4** loaded globally with plugins: \`chartjs-plugin-datalabels\`, \`chartjs-plugin-annotation\`
- Helper functions: \`createGradient(ctx, colorStops)\`, \`animateValue(el, start, end, duration)\`, \`sparklineSVG(data, color, w, h)\`
- CSS classes: \`.glass\`, \`.gradient-text\`, \`.animate-in\` (staggered delays for 12 children), \`.pulse-live\`, \`.counter\`
- Mesh gradient background already applied to body
- Dark theme: body background is #18181b, text is #fafafa
- Chart.js defaults: color=#a1a1aa, borderColor=#27272a, bar borderRadius=6, line tension=0.4, easeOutQuart animation, datalabels off by default

Output a single HTML fragment (no <html>, <head>, or <body> tags). Just the content divs + any inline <script> tags for charts.

## Layout — Bento Grid

Use bento grid layout for visual interest:
  Container: \`grid grid-cols-4 md:grid-cols-6 lg:grid-cols-12 auto-rows-[80px] gap-3\`
  Feature card:   col-span-6 row-span-3
  Metric card:    col-span-3 row-span-2
  Chart:          col-span-6 row-span-4
  Full-width:     col-span-12 row-span-2
  Small status:   col-span-4 row-span-2

Vary card sizes — never make all cards the same size. Mix feature, metric, chart, and small cards.

## Card Styles (3 tiers)

Glass card (hero/primary):  \`class="glass rounded-2xl p-5 animate-in"\`
Accent card (lists/status): \`class="bg-zinc-900/60 border-l-2 border-indigo-500 rounded-xl p-4 animate-in"\`
Subtle card (secondary):    \`class="bg-zinc-900/30 rounded-lg p-3 animate-in"\`

## Typography

Page title:    \`text-3xl font-bold gradient-text\`
Section head:  \`text-lg font-semibold text-zinc-100 tracking-tight\`
Metric value:  \`text-4xl font-bold text-zinc-50 tabular-nums\`
Metric label:  \`text-xs font-medium text-zinc-500 uppercase tracking-wider\`
Body:          \`text-sm text-zinc-300 leading-relaxed\`
Badge/pill:    \`text-xs font-medium px-2.5 py-0.5 rounded-full bg-{color}-500/20 text-{color}-400\`
Trend up:      \`text-emerald-400\` with arrow up
Trend down:    \`text-rose-400\` with arrow down

## Charts — USE CHARTS LIBERALLY

Chart.js 4 + plugins are pre-loaded. Use charts whenever data has quantities, comparisons, trends, or distributions.

### Bar Chart (with gradient + datalabels)
\`\`\`html
<div class="glass rounded-2xl p-5 animate-in">
  <h3 class="text-xs font-medium text-zinc-500 uppercase tracking-wider mb-3">Monthly Revenue</h3>
  <canvas id="barChart1" height="200"></canvas>
</div>
<script>
(function() {
  var ctx = document.getElementById('barChart1').getContext('2d');
  new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Jan', 'Feb', 'Mar', 'Apr'],
      datasets: [{
        data: [12, 19, 8, 15],
        backgroundColor: createGradient(ctx, ['rgba(99,102,241,0.8)', 'rgba(99,102,241,0.2)'])
      }]
    },
    options: { responsive: true, plugins: { legend: { display: false }, datalabels: { display: true, color: '#a1a1aa', anchor: 'end', align: 'top', font: { size: 11 } } }, scales: { y: { grid: { color: '#27272a' } }, x: { grid: { display: false } } } }
  });
})();
</script>
\`\`\`

### Line Chart (with gradient fill)
\`\`\`html
<canvas id="lineChart1" height="200"></canvas>
<script>
(function() {
  var ctx = document.getElementById('lineChart1').getContext('2d');
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
      datasets: [{
        data: [65, 59, 80, 81, 56],
        borderColor: '#6366f1',
        backgroundColor: createGradient(ctx, ['rgba(99,102,241,0.4)', 'rgba(99,102,241,0)']),
        fill: true,
        pointBackgroundColor: '#6366f1'
      }]
    },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { grid: { color: '#27272a' } }, x: { grid: { display: false } } } }
  });
})();
</script>
\`\`\`

### Doughnut Chart (with center text)
\`\`\`html
<div class="relative" style="max-width:220px;margin:0 auto">
  <canvas id="doughnut1" height="220"></canvas>
  <div class="absolute inset-0 flex flex-col items-center justify-center">
    <span class="text-3xl font-bold text-zinc-50">87%</span>
    <span class="text-xs text-zinc-500">Uptime</span>
  </div>
</div>
<script>
new Chart(document.getElementById('doughnut1'), {
  type: 'doughnut',
  data: { labels: ['Healthy', 'Warning', 'Down'], datasets: [{ data: [8, 2, 1], backgroundColor: ['#22c55e', '#eab308', '#ef4444'], borderWidth: 0 }] },
  options: { responsive: true, cutout: '70%', plugins: { legend: { position: 'bottom', labels: { padding: 16 } } } }
});
</script>
\`\`\`

### Gauge Chart (half-doughnut)
\`\`\`html
<div class="relative" style="max-width:200px;margin:0 auto">
  <canvas id="gauge1" height="120"></canvas>
  <div class="absolute bottom-0 left-0 right-0 text-center">
    <span class="text-2xl font-bold text-zinc-50">72%</span>
  </div>
</div>
<script>
new Chart(document.getElementById('gauge1'), {
  type: 'doughnut',
  data: { datasets: [{ data: [72, 28], backgroundColor: ['#6366f1', '#27272a'], borderWidth: 0 }] },
  options: { rotation: -90, circumference: 180, cutout: '75%', responsive: true, plugins: { legend: { display: false }, tooltip: { enabled: false } } }
});
</script>
\`\`\`

### Radar Chart
\`\`\`html
<canvas id="radar1" height="250"></canvas>
<script>
new Chart(document.getElementById('radar1'), {
  type: 'radar',
  data: {
    labels: ['Speed', 'Reliability', 'Uptime', 'Coverage', 'Quality'],
    datasets: [{ data: [85, 92, 78, 90, 88], backgroundColor: 'rgba(99,102,241,0.2)', borderColor: '#6366f1', pointBackgroundColor: '#6366f1' }]
  },
  options: { responsive: true, scales: { r: { grid: { color: '#27272a' }, angleLines: { color: '#27272a' }, ticks: { display: false }, suggestedMin: 0, suggestedMax: 100 } }, plugins: { legend: { display: false } } }
});
</script>
\`\`\`

### Horizontal Bar Chart (for ranked lists)
\`\`\`html
<canvas id="hbar1" height="200"></canvas>
<script>
new Chart(document.getElementById('hbar1'), {
  type: 'bar',
  data: {
    labels: ['Email', 'Telegram', 'Voice', 'Web'],
    datasets: [{ data: [42, 38, 15, 8], backgroundColor: ['#6366f1', '#8b5cf6', '#a78bfa', '#c4b5fd'] }]
  },
  options: { indexAxis: 'y', responsive: true, plugins: { legend: { display: false }, datalabels: { display: true, color: '#fafafa', anchor: 'end', align: 'start', font: { weight: '600' } } }, scales: { x: { grid: { color: '#27272a' } }, y: { grid: { display: false } } } }
});
</script>
\`\`\`

### Annotation Line (target/threshold)
Add to any chart's options:
\`\`\`js
plugins: { annotation: { annotations: { target: { type: 'line', yMin: 80, yMax: 80, borderColor: '#22c55e', borderDash: [5,5], label: { content: 'Target', display: true, color: '#22c55e', font: { size: 11 } } } } } }
\`\`\`

## Data Display Patterns

### Metric Card with Sparkline
\`\`\`html
<div class="glass rounded-2xl p-5 animate-in">
  <p class="text-xs font-medium text-zinc-500 uppercase tracking-wider">Revenue</p>
  <div class="flex items-end justify-between mt-2">
    <p class="text-4xl font-bold text-zinc-50 tabular-nums" id="metric1">0</p>
    <div id="spark1"></div>
  </div>
  <p class="text-xs text-emerald-400 mt-1">&#8593; 12% from last week</p>
</div>
<script>
animateValue(document.getElementById('metric1'), 0, 42850, 1500);
document.getElementById('spark1').innerHTML = sparklineSVG([35,38,42,39,41,44,43], '#22c55e', 80, 24);
</script>
\`\`\`

### Service Status Row
\`\`\`html
<div class="flex items-center gap-3 py-2">
  <div class="w-2 h-2 rounded-full bg-green-500 pulse-live"></div>
  <span class="text-sm text-zinc-300 flex-1">Vision Service</span>
  <span class="text-xs font-medium px-2.5 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">99.9%</span>
</div>
\`\`\`

### Progress Bar (gradient)
\`\`\`html
<div class="w-full bg-zinc-800 rounded-full h-2">
  <div class="h-2 rounded-full bg-gradient-to-r from-indigo-500 to-purple-500" style="width: 72%"></div>
</div>
\`\`\`

### Table
\`\`\`html
<div class="overflow-auto">
  <table class="w-full text-sm">
    <thead><tr class="border-b border-zinc-800 sticky top-0 bg-zinc-900/80"><th class="text-left py-2 text-xs font-medium text-zinc-500 uppercase">Name</th><th class="text-right py-2 text-xs font-medium text-zinc-500 uppercase">Value</th></tr></thead>
    <tbody>
      <tr class="border-b border-zinc-800/50 hover:bg-zinc-800/50"><td class="py-2 text-zinc-300">Item</td><td class="py-2 text-right text-zinc-100 tabular-nums">1,234</td></tr>
    </tbody>
  </table>
</div>
\`\`\`

### Badge System
Green (healthy): \`bg-emerald-500/20 text-emerald-400\`
Yellow (warning): \`bg-yellow-500/20 text-yellow-400\`
Red (critical):   \`bg-rose-500/20 text-rose-400\`
Blue (info):      \`bg-blue-500/20 text-blue-400\`
Always: \`text-xs font-medium px-2.5 py-0.5 rounded-full\`

## Rules
1. Output ONLY HTML + inline scripts — no markdown, no explanation, no code fences
2. Keep it under 350 lines
3. **Use bento grid** — vary card sizes for visual interest
4. **Use glass cards** for hero metrics, accent cards for lists
5. **Every dashboard MUST have at least one chart** — prefer charts over tables/text where data has numbers
6. Use \`animateValue()\` for hero metric numbers
7. Use \`sparklineSVG()\` for inline trend indicators
8. Use \`createGradient()\` for area/line chart fills
9. Use \`.gradient-text\` on the main dashboard heading
10. Use \`animate-in\` on all cards (staggering is automatic)
11. Use badges/pills for status values, not plain text
12. Give each canvas a unique id
13. Use Chart.js charts liberally — bar, line, doughnut, gauge, radar, horizontal bar
14. Use data from the conversation and pre-fetched context — do NOT call fetch() or external APIs
15. Do NOT add <script src="..."> tags — libraries are already loaded`;
}

/**
 * Build an enriched prompt for the new triage-driven pipeline.
 * Takes the triage summary (what to show) + enriched data (real data to display).
 * The triage summary replaces hardcoded topic instructions — it's dynamic
 * and specific to each conversation.
 */
export function buildEnrichedPrompt(
  messages: Array<{ role: string; content: string }>,
  data: Record<string, unknown>,
  triageSummary: string
): string {
  const parts: string[] = [];

  parts.push(`Generate a dashboard that visualizes: "${triageSummary}"`);
  parts.push(`\nAnalyze the data and conversation below, then create the most appropriate visualization.
Choose the best chart types, card layouts, and metrics to represent this information clearly.`);

  parts.push("\n## Conversation");
  for (const msg of messages.slice(-4)) {
    parts.push(`${msg.role === "user" ? "User" : "Assistant"}: ${msg.content}`);
  }

  if (data && Object.keys(data).length > 0) {
    const dataStr = JSON.stringify(data, null, 2).slice(0, 6000);
    parts.push(`\n## Available Data (use this real data to populate the dashboard)\n${dataStr}`);
  }

  parts.push(`\n## Rendering Rules
- **Use bento grid layout** with varied card sizes
- **Use glass cards** for hero/primary metrics, accent cards for lists and status
- **Use animated counters** — \`animateValue(el, 0, value, 1500)\` for hero numbers
- **Use sparklines** — \`sparklineSVG(data, color)\` for inline trend data
- **Use .gradient-text** on the main heading
- Use \`createGradient()\` for chart fills, badges/pills for status values
- Use real values from the data AND conversation — never show placeholder or empty data
- Make it information-dense, visually impressive, and polished
- Include at least one chart — find a way to visualize data graphically
- Output HTML + inline scripts only. No markdown fences, no explanation.`);

  return parts.join("\n");
}

/** Time-aware prompt additions for welcome dashboards */
export function getTimeAwarePrompt(hour: number): string {
  if (hour >= 6 && hour < 11) {
    return `Generate a MORNING dashboard with:
- A warm greeting "Good morning, Philip" as a large heading with gradient-text class
- Today's date below the greeting
- Use a glass card for the greeting hero section
- Include a gauge chart for overall service health percentage
- A metrics row with animated counters showing service health counts and inbox counts
- Service status indicators with pulse-live dots and uptime badges
- If calendar data is provided, show it in an accent card section
- Use warm indigo/purple gradients on charts
Tone: Fresh, clean, focused on the day ahead.`;
  }
  if (hour >= 11 && hour < 17) {
    return `Generate a MIDDAY dashboard with:
- A brief status heading like "Afternoon, Philip" with gradient-text class
- Compact bento layout — mix metric and status cards
- Sparklines for quick at-a-glance trend metrics
- Accent-border cards for status lists
- Service health as small colored dots with pulse-live animation
- If there are open conversations, list them in an accent card
- Include at least one chart — bar chart for counts or doughnut for distribution
Tone: Productive, compact, no fluff.`;
  }
  if (hour >= 17 && hour < 22) {
    return `Generate an EVENING dashboard with:
- A relaxed heading "Evening, Philip" with gradient-text class
- Relaxed spacing between cards
- Today's summary in glass cards with softer colors (zinc-700 borders)
- A summary doughnut chart for service health distribution
- Glass card for the main status message
- If there are tomorrow items, show them in a subtle card
Tone: Relaxed, reflective.`;
  }
  // Night (22-6)
  return `Generate a MINIMAL night dashboard with:
- Brief heading "Hey Philip" with gradient-text class
- A single glass card with service health
- Use a gauge chart if showing health percentage
- Very low visual noise — minimal cards, muted colors
Tone: Quiet, dark, minimal.`;
}
