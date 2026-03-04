const http = require('http');
const fs = require('fs');
const { execSync, exec: execCb } = require('child_process');
const { brandVars, brandBase } = require('../shared/brand-styles');

const PORT = 3001;
const MEMORY_DIR = '/home/philip/clawd/memory';

// ─── Health check definitions ───────────────────────────────────────────────

const SERVICE_DEFS = [
  {
    id: 'agent-engine',
    name: 'Agent Engine',
    icon: '🧠',
    check: 'http',
    url: 'http://localhost:18800/health',
  },
  {
    id: 'rag-orchestrator',
    name: 'RAG Orchestrator',
    icon: '🔍',
    check: 'http',
    url: 'http://localhost:9099/health',
  },
  {
    id: 'ollama',
    name: 'Ollama',
    icon: '🦙',
    check: 'http',
    url: 'http://localhost:11434/api/tags',
  },
  {
    id: 'voice-server',
    name: 'Voice Server',
    icon: '📞',
    check: 'port',
    port: 8765,
  },
  {
    id: 'bridge-service',
    name: 'Bridge Service',
    icon: '🔗',
    check: 'http',
    url: 'http://localhost:9100/health',
  },
  {
    id: 'cloudflare-tunnel',
    name: 'Cloudflare Tunnel',
    icon: '🌐',
    check: 'systemd',
    unit: 'cloudflared',
  },
  {
    id: 'calendar-sync',
    name: 'Calendar Sync',
    icon: '📅',
    check: 'freshness',
    file: `${MEMORY_DIR}/calendar-log.json`,
    field: 'lastCheckedAt',
    staleDayMins: 15,
    staleNightMins: 60,
  },
  {
    id: 'email-sync',
    name: 'Email Sync',
    icon: '📧',
    check: 'freshness',
    file: `${MEMORY_DIR}/email-log.json`,
    field: 'lastCheckedAt',
    staleDayMins: 15,
    staleNightMins: 60,
  },
  {
    id: 'triage-worker',
    name: 'Triage Worker',
    icon: '⚙️',
    check: 'freshness',
    file: `${MEMORY_DIR}/worker-handoff.json`,
    field: 'lastRunAt',
    staleDayMins: 20,
    staleNightMins: 60,
  },
  {
    id: 'supervisor-heartbeat',
    name: 'Supervisor Heartbeat',
    icon: '💓',
    check: 'freshness',
    file: `${MEMORY_DIR}/worker-handoff.json`,
    field: 'lastRunAt',
    staleDayMins: 25,
    staleNightMins: 90,
  },
  {
    id: 'mediamtx-webcam',
    name: 'Vision',
    icon: '👁️',
    check: 'rtsp',
    url: 'rtsp://localhost:8554/webcam',
  },
];

// ─── Check functions ────────────────────────────────────────────────────────

function httpCheck(url, timeoutMs = 5000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      let body = '';
      res.on('data', (c) => (body += c));
      res.on('end', () => {
        const ms = Date.now() - start;
        const ok = res.statusCode >= 200 && res.statusCode < 500;
        resolve({ status: ok ? 'up' : 'degraded', responseMs: ms });
      });
    });
    req.on('error', () => resolve({ status: 'down', responseMs: null }));
    req.on('timeout', () => {
      req.destroy();
      resolve({ status: 'down', responseMs: null });
    });
  });
}

function portCheck(port, timeoutMs = 3000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const net = require('net');
    const sock = new net.Socket();
    sock.setTimeout(timeoutMs);
    sock.on('connect', () => {
      const ms = Date.now() - start;
      sock.destroy();
      resolve({ status: 'up', responseMs: ms });
    });
    sock.on('error', () => {
      sock.destroy();
      resolve({ status: 'down', responseMs: null });
    });
    sock.on('timeout', () => {
      sock.destroy();
      resolve({ status: 'down', responseMs: null });
    });
    sock.connect(port, '127.0.0.1');
  });
}

function systemdCheck(unit) {
  try {
    const result = execSync(`systemctl is-active ${unit}`, {
      encoding: 'utf8',
      timeout: 3000,
    }).trim();
    return { status: result === 'active' ? 'up' : 'degraded', responseMs: null };
  } catch {
    return { status: 'down', responseMs: null };
  }
}

function rtspCheck(url, timeoutMs = 10000) {
  return new Promise((resolve) => {
    const start = Date.now();
    execCb(
      `ffmpeg -rtsp_transport tcp -i ${url} -frames:v 1 -update 1 -y /tmp/webcam-status-check.jpg`,
      { timeout: timeoutMs },
      (err) => {
        const ms = Date.now() - start;
        if (err) {
          resolve({ status: 'down', responseMs: ms });
        } else {
          resolve({ status: 'up', responseMs: ms, detail: 'stream active' });
        }
      }
    );
  });
}

function freshnessCheck(filePath, field, staleDayMins, staleNightMins) {
  try {
    const data = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    const val = data[field];
    if (!val) return { status: 'down', responseMs: null, detail: 'no timestamp' };

    const ts = new Date(val);
    const now = new Date();
    const ageMins = (now - ts) / 60000;

    // Determine if daytime (8am-10pm ET)
    const etHour = parseInt(
      now.toLocaleString('en-US', { hour: 'numeric', hour12: false, timeZone: 'America/New_York' })
    );
    const isDaytime = etHour >= 8 && etHour < 22;
    const threshold = isDaytime ? staleDayMins : staleNightMins;

    let status = 'up';
    if (ageMins > threshold * 2) status = 'down';
    else if (ageMins > threshold) status = 'degraded';

    return {
      status,
      responseMs: null,
      detail: `${Math.round(ageMins)}m ago`,
      lastValue: val,
    };
  } catch (e) {
    return { status: 'down', responseMs: null, detail: e.message };
  }
}

async function checkService(def) {
  const base = { id: def.id, name: def.name, icon: def.icon, checkedAt: new Date().toISOString() };
  let result;

  switch (def.check) {
    case 'http':
      result = await httpCheck(def.url);
      break;
    case 'port':
      result = await portCheck(def.port);
      break;
    case 'systemd':
      result = systemdCheck(def.unit);
      break;
    case 'freshness':
      result = freshnessCheck(def.file, def.field, def.staleDayMins, def.staleNightMins);
      break;
    case 'rtsp':
      result = await rtspCheck(def.url);
      break;
    default:
      result = { status: 'down', responseMs: null };
  }

  return { ...base, ...result };
}

// ─── Extra data helpers ─────────────────────────────────────────────────────

function getActiveEscalations() {
  try {
    const data = JSON.parse(fs.readFileSync(`${MEMORY_DIR}/worker-handoff.json`, 'utf8'));
    const all = data.escalations || [];
    return {
      total: all.length,
      active: all.filter((e) => !e.resolvedAt).length,
    };
  } catch {
    return { total: 0, active: 0 };
  }
}

function getNextEvent() {
  try {
    const data = JSON.parse(fs.readFileSync(`${MEMORY_DIR}/calendar-log.json`, 'utf8'));
    const now = new Date();
    const upcoming = (data.meetings || [])
      .filter((m) => new Date(m.start) > now)
      .sort((a, b) => new Date(a.start) - new Date(b.start));
    if (upcoming.length === 0) return null;
    const e = upcoming[0];
    return { title: e.title, start: e.start, location: e.location || null };
  } catch {
    return null;
  }
}

function getUptime() {
  try {
    return execSync('uptime -p', { encoding: 'utf8' }).trim().replace('up ', '');
  } catch {
    return 'unknown';
  }
}

// ─── Aggregate status ───────────────────────────────────────────────────────

async function getAllStatus() {
  const services = await Promise.all(SERVICE_DEFS.map(checkService));
  const escalations = getActiveEscalations();
  const nextEvent = getNextEvent();
  const uptime = getUptime();

  const downCount = services.filter((s) => s.status === 'down').length;
  const degradedCount = services.filter((s) => s.status === 'degraded').length;

  let overall = 'All Systems Operational';
  let overallClass = 'ok';
  if (downCount >= 3) {
    overall = 'Major Outage';
    overallClass = 'major';
  } else if (downCount > 0 || degradedCount > 0) {
    overall = 'Partial Outage';
    overallClass = 'partial';
  }

  return { services, overall, overallClass, escalations, nextEvent, uptime, checkedAt: new Date().toISOString() };
}

// ─── HTML page ──────────────────────────────────────────────────────────────

function renderPage() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Robothor Status Dashboard</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📊</text></svg>">
  <style>
    ${brandVars()}
    ${brandBase()}

    /* Legacy aliases for JS references below */
    :root {
      --success: oklch(0.696 0.17 162.48);
      --warning: oklch(0.769 0.188 70.08);
      --danger: oklch(0.645 0.246 16.439);
      --muted: oklch(0.45 0.01 286);
      --text: var(--r-text);
      --text2: var(--r-text-muted);
      --text3: var(--r-text-dim);
      --accent: var(--r-primary);
    }

    .container { max-width:1000px; margin:0 auto; padding:2rem; }

    /* ── Header ── */
    .header {
      text-align:center;
      padding:2.5rem 2rem 2rem;
      margin-bottom:1.5rem;
    }
    .header h1 {
      font-size:2.2rem; font-weight:700;
      background:linear-gradient(135deg,var(--accent),var(--accent2),var(--accent3));
      -webkit-background-clip:text; -webkit-text-fill-color:transparent;
      letter-spacing:-0.5px; margin-bottom:0.5rem;
    }
    .header .subtitle { color:var(--text3); font-size:0.95rem; }

    /* ── Overall banner ── */
    .overall-banner {
      display:flex; align-items:center; justify-content:center; gap:0.75rem;
      padding:0.8rem 1.6rem; border-radius:16px; margin:1.5rem auto; width:fit-content;
      font-size:1.05rem; font-weight:600;
    }
    .overall-banner.ok {
      background:rgba(36,224,138,0.1); border:1px solid rgba(36,224,138,0.3); color:var(--success);
    }
    .overall-banner.partial {
      background:rgba(240,192,64,0.1); border:1px solid rgba(240,192,64,0.3); color:var(--warning);
    }
    .overall-banner.major {
      background:rgba(239,68,68,0.1); border:1px solid rgba(239,68,68,0.3); color:var(--danger);
    }

    .overall-dot {
      width:10px; height:10px; border-radius:50%;
    }
    .overall-banner.ok .overall-dot { background:var(--success); box-shadow:0 0 12px var(--success); animation:pulse 2s infinite; }
    .overall-banner.partial .overall-dot { background:var(--warning); box-shadow:0 0 12px var(--warning); animation:pulse 2s infinite; }
    .overall-banner.major .overall-dot { background:var(--danger); box-shadow:0 0 12px var(--danger); animation:pulse 1s infinite; }

    @keyframes pulse {
      0%,100% { opacity:1; transform:scale(1); }
      50% { opacity:0.5; transform:scale(0.85); }
    }

    .meta-row {
      display:flex; justify-content:center; gap:1.5rem; flex-wrap:wrap;
      color:var(--text3); font-size:0.82rem; margin-top:0.75rem;
    }
    .meta-row span { display:flex; align-items:center; gap:0.3rem; }

    /* ── Info cards row ── */
    .info-row {
      display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
      gap:1rem; margin-bottom:1.5rem;
    }
    .info-card {
      padding:1.2rem 1.4rem; border-radius:16px; text-align:center;
    }
    .info-card .info-value {
      font-size:1.6rem; font-weight:700;
      background:linear-gradient(135deg,var(--accent),var(--accent3));
      -webkit-background-clip:text; -webkit-text-fill-color:transparent;
    }
    .info-card .info-label {
      font-size:0.7rem; color:var(--text3); text-transform:uppercase; letter-spacing:1px; margin-top:0.2rem;
    }
    .info-card .info-sub {
      font-size:0.75rem; color:var(--text2); margin-top:0.35rem;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    }

    /* ── Services grid ── */
    .services-grid {
      display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
      gap:1rem; margin-bottom:1.5rem;
    }

    .svc-card {
      padding:1.2rem 1.4rem; border-radius:16px;
      display:flex; align-items:flex-start; gap:1rem;
      transition:transform 0.15s, border-color 0.15s;
    }
    .svc-card:hover { transform:translateY(-2px); }

    .svc-icon { font-size:1.5rem; flex-shrink:0; margin-top:2px; }

    .svc-info { flex:1; min-width:0; }
    .svc-name { font-weight:600; font-size:0.95rem; margin-bottom:0.35rem; display:flex; align-items:center; gap:0.5rem; }

    .svc-badge {
      font-size:0.6rem; font-weight:700; text-transform:uppercase;
      padding:0.15rem 0.5rem; border-radius:8px; letter-spacing:0.5px;
    }
    .svc-badge.up { background:rgba(36,224,138,0.15); color:var(--success); }
    .svc-badge.degraded { background:rgba(240,192,64,0.15); color:var(--warning); }
    .svc-badge.down { background:rgba(239,68,68,0.15); color:var(--danger); }
    .svc-badge.off { background:rgba(100,116,139,0.15); color:var(--muted); }

    .svc-meta { display:flex; gap:1rem; font-size:0.75rem; color:var(--text3); flex-wrap:wrap; }

    .svc-dot {
      width:8px; height:8px; border-radius:50%; flex-shrink:0; margin-top:7px;
    }
    .svc-dot.up { background:var(--success); box-shadow:0 0 8px var(--success); animation:pulse 2s infinite; }
    .svc-dot.degraded { background:var(--warning); box-shadow:0 0 8px var(--warning); }
    .svc-dot.down { background:var(--danger); box-shadow:0 0 6px var(--danger); }
    .svc-dot.off { background:var(--muted); }

    /* ── Footer ── */
    .footer {
      text-align:center; padding:2rem; color:var(--text3); font-size:0.85rem;
    }
    .footer a { color:var(--accent); text-decoration:none; }
    .footer a:hover { text-decoration:underline; }

    .refresh-note {
      text-align:center; color:var(--text3); font-size:0.75rem; margin-bottom:1rem;
    }
    .refresh-note span { cursor:pointer; color:var(--accent); }
    .refresh-note span:hover { text-decoration:underline; }

    @media (max-width:600px) {
      .container { padding:1rem; }
      .header h1 { font-size:1.6rem; }
      .services-grid { grid-template-columns:1fr; }
      .info-row { grid-template-columns:1fr 1fr; }
    }
  </style>
</head>
<body>
  <div class="bg-gradient"></div>
  <div class="container">

    <section class="header glass">
      <h1>⚡ Robothor Status</h1>
      <p class="subtitle">Operational Status Dashboard</p>

      <div id="banner" class="overall-banner ok">
        <span class="overall-dot"></span>
        <span id="banner-text">Checking…</span>
      </div>

      <div class="meta-row">
        <span>🕐 <span id="meta-time"></span></span>
        <span>⏱ Up <span id="meta-uptime">…</span></span>
        <span>🔄 Auto-refresh 30 s</span>
      </div>
    </section>

    <div class="info-row" id="info-row">
      <div class="info-card glass">
        <div class="info-value" id="esc-count">–</div>
        <div class="info-label">Active Escalations</div>
      </div>
      <div class="info-card glass">
        <div class="info-value" id="svc-up-count">–</div>
        <div class="info-label">Services Healthy</div>
      </div>
      <div class="info-card glass">
        <div class="info-value" id="next-event-time">–</div>
        <div class="info-label">Next Event</div>
        <div class="info-sub" id="next-event-title"></div>
      </div>
    </div>

    <div class="refresh-note">
      Last checked: <span id="last-checked">–</span> · <span onclick="refresh()">Refresh now</span>
    </div>

    <div class="services-grid" id="services-grid"></div>

    <footer class="footer">
      <p style="margin-bottom:0.4rem;">Robothor Status Dashboard</p>
      <p>Powered by <a href="https://ironsail.ai">Ironsail</a> · Robothor Engine</p>
      <p style="margin-top:0.6rem;"><a href="https://robothor.ai">← robothor.ai</a></p>
    </footer>
  </div>

<script>
let lastData = null;

function ago(iso) {
  if (!iso) return '—';
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s/60) + 'm ago';
  return Math.round(s/3600) + 'h ago';
}

function fmtMs(ms) {
  if (ms == null) return '—';
  return ms + ' ms';
}

function fmtTime(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-US', {
    weekday:'short', month:'short', day:'numeric',
    hour:'numeric', minute:'2-digit',
    timeZone:'America/New_York'
  });
}

function renderServices(services) {
  const grid = document.getElementById('services-grid');
  grid.innerHTML = services.map(s => {
    const st = s.status;
    const borderColor = st==='up' ? 'rgba(36,224,138,0.2)' : st==='degraded' ? 'rgba(240,192,64,0.2)' : st==='down' ? 'rgba(239,68,68,0.2)' : 'rgba(100,116,139,0.15)';
    return '<div class="svc-card glass" style="border-color:'+borderColor+'">'
      + '<div class="svc-dot '+st+'"></div>'
      + '<div class="svc-info">'
      + '  <div class="svc-name">'+s.icon+' '+s.name+' <span class="svc-badge '+st+'">'+st.toUpperCase()+'</span></div>'
      + '  <div class="svc-meta">'
      + (s.responseMs != null ? '<span>⏱ '+fmtMs(s.responseMs)+'</span>' : '')
      + (s.detail ? '<span>📋 '+s.detail+'</span>' : '')
      + '<span>🔄 '+ago(s.checkedAt)+'</span>'
      + '  </div>'
      + '</div>'
      + '</div>';
  }).join('');
}

function renderData(data) {
  lastData = data;
  // Banner
  const banner = document.getElementById('banner');
  banner.className = 'overall-banner ' + data.overallClass;
  document.getElementById('banner-text').textContent = data.overall;

  // Meta
  const now = new Date();
  document.getElementById('meta-time').textContent = now.toLocaleString('en-US',{hour:'numeric',minute:'2-digit',timeZone:'America/New_York'}) + ' EST';
  document.getElementById('meta-uptime').textContent = data.uptime;

  // Info cards
  document.getElementById('esc-count').textContent = data.escalations.active;
  const upCount = data.services.filter(s => s.status==='up').length;
  document.getElementById('svc-up-count').textContent = upCount + '/' + data.services.length;

  if (data.nextEvent) {
    document.getElementById('next-event-time').textContent = fmtTime(data.nextEvent.start);
    document.getElementById('next-event-title').textContent = data.nextEvent.title;
  } else {
    document.getElementById('next-event-time').textContent = 'None';
    document.getElementById('next-event-title').textContent = '';
  }

  // Last checked
  document.getElementById('last-checked').textContent = ago(data.checkedAt);

  // Service cards
  renderServices(data.services);
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    renderData(data);
  } catch (e) {
    console.error('Fetch failed', e);
  }
}

// Update "ago" times every 5s without re-fetching
setInterval(() => {
  if (lastData) {
    document.getElementById('last-checked').textContent = ago(lastData.checkedAt);
    // Re-render service ago times
    renderServices(lastData.services);
  }
}, 5000);

// Full refresh every 30s
setInterval(refresh, 30000);

// Initial load
refresh();
</script>
</body>
</html>`;
}

// ─── HTTP server ────────────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  // CORS headers for API
  res.setHeader('Access-Control-Allow-Origin', '*');

  const url = req.url.split('?')[0];

  if (url === '/' || url === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(renderPage());
  } else if (url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', timestamp: Date.now() }));
  } else if (url === '/api/webcam') {
    // Serve a live webcam snapshot
    const snapPath = '/tmp/webcam-dashboard-snap.jpg';
    execCb(
      `ffmpeg -rtsp_transport tcp -i rtsp://localhost:8554/webcam -frames:v 1 -update 1 -y ${snapPath}`,
      { timeout: 8000 },
      (err) => {
        if (err) {
          res.writeHead(503, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'webcam unavailable' }));
        } else {
          try {
            const img = fs.readFileSync(snapPath);
            res.writeHead(200, {
              'Content-Type': 'image/jpeg',
              'Cache-Control': 'no-cache, no-store',
              'X-Timestamp': new Date().toISOString(),
            });
            res.end(img);
          } catch (e) {
            res.writeHead(500, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: e.message }));
          }
        }
      }
    );
    return;
  } else if (url === '/api/status') {
    const data = await getAllStatus();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
  } else {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  }
});

server.listen(PORT, () => {
  console.log(`Robothor Status Dashboard running at http://localhost:${PORT}`);
});
