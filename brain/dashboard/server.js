const http = require('http');
const fs = require('fs');
const path = require('path');
const { execSync, exec } = require('child_process');
const { brandVars, brandBase } = require('../shared/brand-styles');

const PORT = process.env.PORT || 3003;
const MEMORY_DIR = '/home/philip/robothor/brain/memory';
const LOGS_DIR = '/home/philip/robothor/brain/memory_system/logs';

// ============ Data Loaders ============

function loadJSON(filepath) {
  try {
    if (fs.existsSync(filepath)) {
      return JSON.parse(fs.readFileSync(filepath, 'utf8'));
    }
  } catch (e) {
    console.error(`Error loading ${filepath}:`, e.message);
  }
  return null;
}

function getEmails() {
  const data = loadJSON(path.join(MEMORY_DIR, 'email-log.json'));
  if (!data) return { lastChecked: null, emails: [], stats: {} };
  
  const entries = Object.values(data.entries || {});
  const unread = entries.filter(e => !e.reviewedAt);
  const urgent = entries.filter(e => e.urgency === 'high' || e.urgency === 'critical');
  
  return {
    lastChecked: data.lastCheckedAt,
    emails: entries.slice(-20).reverse(),
    stats: {
      total: entries.length,
      unread: unread.length,
      urgent: urgent.length
    }
  };
}

function getCalendar() {
  const data = loadJSON(path.join(MEMORY_DIR, 'calendar-log.json'));
  if (!data) return { lastChecked: null, meetings: [], changes: [] };
  
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  
  const todayMeetings = (data.meetings || []).filter(m => 
    m.start && m.start.startsWith(today)
  ).sort((a, b) => a.start.localeCompare(b.start));
  
  const upcoming = (data.meetings || []).filter(m => {
    if (!m.start) return false;
    const start = new Date(m.start);
    const diff = (start - now) / 60000; // minutes
    return diff > 0 && diff <= 60;
  });
  
  return {
    lastChecked: data.lastCheckedAt,
    meetings: todayMeetings,
    upcoming,
    changes: (data.changes || []).slice(-10).reverse()
  };
}

function getTasks() {
  const data = loadJSON(path.join(MEMORY_DIR, 'tasks.json'));
  if (!data) return { tasks: [], stats: {} };
  
  const tasks = data.tasks || [];
  const pending = tasks.filter(t => t.status === 'pending');
  const inProgress = tasks.filter(t => t.status === 'in_progress');
  const completed = tasks.filter(t => t.status === 'completed').slice(-5);
  
  return {
    tasks,
    pending,
    inProgress,
    completed,
    stats: {
      total: tasks.length,
      pending: pending.length,
      inProgress: inProgress.length
    }
  };
}

function getJira() {
  const data = loadJSON(path.join(MEMORY_DIR, 'jira-log.json'));
  if (!data) return { lastSync: null, tickets: [], pending: [] };
  
  return {
    lastSync: data.lastSyncAt,
    status: data.lastSyncStatus,
    tickets: Object.values(data.activeTickets || {}),
    pending: data.pendingActions || [],
    history: (data.syncHistory || []).slice(0, 5)
  };
}

function getSecurity() {
  const data = loadJSON(path.join(MEMORY_DIR, 'security-log.json'));
  if (!data) return { entries: [], unreviewed: 0 };
  
  const entries = data.entries || [];
  const unreviewed = entries.filter(e => !e.reviewedAt).length;
  
  return { entries: entries.slice(-10), unreviewed };
}

function getCronStatus() {
  // Read from crontab
  let systemCrons = [];
  try {
    const crontab = execSync('crontab -l 2>/dev/null', { encoding: 'utf8' });
    const lines = crontab.split('\n').filter(l => l.trim() && !l.startsWith('#'));
    systemCrons = lines.map(l => {
      const match = l.match(/^([^\s]+\s+[^\s]+\s+[^\s]+\s+[^\s]+\s+[^\s]+)\s+(.+)$/);
      if (match) {
        const script = match[2].split('/').pop().split(' ')[0];
        return { schedule: match[1], command: script };
      }
      return { schedule: '?', command: l.slice(0, 50) };
    });
  } catch {}
  
  // Get log timestamps
  const logs = {};
  ['email-sync.log', 'calendar-sync.log', 'jira-sync.log'].forEach(log => {
    try {
      const stat = fs.statSync(path.join(LOGS_DIR, log));
      logs[log] = stat.mtime.toISOString();
    } catch {}
  });
  
  return { systemCrons, logs };
}

function getHeartbeatStatus() {
  try {
    const result = execSync('curl -s http://localhost:18800/health', { encoding: 'utf8', timeout: 2000 });
    return JSON.parse(result);
  } catch {
    return { status: 'unknown' };
  }
}

function getServices() {
  const services = [
    { name: 'Engine', port: 18800 },
    { name: 'Voice', port: 8765 },
    { name: 'RAG', port: 9099 },
    { name: 'Status', port: 3000 },
  ];
  
  return services.map(s => {
    try {
      execSync(`curl -s --max-time 1 http://localhost:${s.port}/health || curl -s --max-time 1 http://localhost:${s.port}/`, { encoding: 'utf8' });
      return { ...s, status: 'up' };
    } catch {
      return { ...s, status: 'down' };
    }
  });
}

function getAllData() {
  return {
    timestamp: new Date().toISOString(),
    emails: getEmails(),
    calendar: getCalendar(),
    tasks: getTasks(),
    jira: getJira(),
    security: getSecurity(),
    crons: getCronStatus(),
    services: getServices()
  };
}

// ============ HTML Template ============

function renderDashboard() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Robothor Dashboard</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
  <style>
    ${brandVars()}
    ${brandBase()}

    /* Legacy aliases */
    :root {
      --bg: var(--r-bg);
      --glass: oklch(1 0 0 / 4%);
      --glass-border: var(--r-border);
      --glass-hover: oklch(1 0 0 / 8%);
      --accent: var(--r-primary);
      --accent2: var(--r-accent);
      --success: oklch(0.696 0.17 162.48);
      --warning: oklch(0.769 0.188 70.08);
      --danger: oklch(0.645 0.246 16.439);
      --text: var(--r-text);
      --text2: var(--r-text-muted);
      --text3: var(--r-text-dim);
    }

    .bg-effects { display: none; /* replaced by .bg-gradient from brandBase */ }
    
    /* Header */
    .header {
      padding: 1rem 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--glass-border);
      position: sticky;
      top: 0;
      background: rgba(10,10,15,0.9);
      backdrop-filter: blur(10px);
      z-index: 100;
    }
    
    .logo {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      font-size: 1.25rem;
      font-weight: 600;
    }
    
    .logo span { font-size: 1.5rem; }
    
    .header-right {
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    
    .live-indicator {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.8rem;
      color: var(--text2);
    }
    
    .live-dot {
      width: 8px;
      height: 8px;
      background: var(--success);
      border-radius: 50%;
      animation: pulse 2s infinite;
    }
    
    @keyframes pulse {
      0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(16,185,129,0.4); }
      50% { opacity: 0.8; box-shadow: 0 0 0 6px rgba(16,185,129,0); }
    }
    
    .time { font-size: 0.85rem; color: var(--text2); }
    
    /* Main Layout */
    .main {
      display: grid;
      grid-template-columns: 1fr 1fr 300px;
      grid-template-rows: auto auto 1fr;
      gap: 1rem;
      padding: 1rem;
      max-width: 1800px;
      margin: 0 auto;
    }
    
    @media (max-width: 1400px) {
      .main { grid-template-columns: 1fr 1fr; }
      .sidebar { grid-column: span 2; }
    }
    
    @media (max-width: 900px) {
      .main { grid-template-columns: 1fr; }
      .sidebar { grid-column: span 1; }
    }
    
    /* Panels */
    .panel {
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
    }
    
    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1rem;
    }
    
    .panel-title {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-weight: 600;
      font-size: 0.95rem;
    }
    
    .panel-badge {
      background: rgba(102,126,234,0.2);
      color: var(--accent);
      padding: 0.2rem 0.6rem;
      border-radius: 10px;
      font-size: 0.7rem;
      font-weight: 600;
    }
    
    .panel-badge.warning { background: rgba(245,158,11,0.2); color: var(--warning); }
    .panel-badge.danger { background: rgba(239,68,68,0.2); color: var(--danger); }
    .panel-badge.success { background: rgba(16,185,129,0.2); color: var(--success); }
    
    .panel-content {
      flex: 1;
      overflow-y: auto;
    }
    
    /* Email Panel */
    .email-item {
      padding: 0.75rem;
      border-radius: 10px;
      margin-bottom: 0.5rem;
      background: rgba(255,255,255,0.02);
      border: 1px solid transparent;
      cursor: pointer;
      transition: all 0.2s;
    }
    
    .email-item:hover {
      background: rgba(255,255,255,0.05);
      border-color: var(--glass-border);
    }
    
    .email-item.unread { border-left: 3px solid var(--accent); }
    .email-item.urgent { border-left: 3px solid var(--danger); }
    
    .email-from {
      font-weight: 500;
      font-size: 0.9rem;
      margin-bottom: 0.25rem;
    }
    
    .email-subject {
      color: var(--text2);
      font-size: 0.8rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    
    .email-meta {
      display: flex;
      justify-content: space-between;
      margin-top: 0.5rem;
      font-size: 0.7rem;
      color: var(--text3);
    }
    
    /* Calendar Panel */
    .meeting-item {
      display: flex;
      gap: 1rem;
      padding: 0.75rem;
      border-radius: 10px;
      margin-bottom: 0.5rem;
      background: rgba(255,255,255,0.02);
    }
    
    .meeting-time {
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--accent);
      min-width: 60px;
    }
    
    .meeting-info { flex: 1; }
    
    .meeting-title {
      font-weight: 500;
      font-size: 0.9rem;
      margin-bottom: 0.25rem;
    }
    
    .meeting-attendees {
      font-size: 0.75rem;
      color: var(--text3);
    }
    
    .meeting-soon {
      background: rgba(245,158,11,0.1);
      border: 1px solid rgba(245,158,11,0.3);
    }
    
    /* Tasks Panel */
    .task-item {
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
      padding: 0.6rem;
      border-radius: 8px;
      margin-bottom: 0.4rem;
      background: rgba(255,255,255,0.02);
    }
    
    .task-status {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-top: 0.4rem;
      flex-shrink: 0;
    }
    
    .task-status.pending { background: var(--warning); }
    .task-status.in_progress { background: var(--accent); }
    .task-status.completed { background: var(--success); }
    
    .task-text {
      font-size: 0.85rem;
      color: var(--text2);
    }
    
    .task-meta {
      font-size: 0.7rem;
      color: var(--text3);
      margin-top: 0.25rem;
    }
    
    /* Services Panel */
    .services-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.5rem;
    }
    
    .service-item {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.6rem;
      border-radius: 8px;
      background: rgba(255,255,255,0.02);
      font-size: 0.85rem;
    }
    
    .service-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }
    
    .service-dot.up { background: var(--success); }
    .service-dot.down { background: var(--danger); }
    
    /* Crons Panel */
    .cron-item {
      display: flex;
      justify-content: space-between;
      padding: 0.5rem;
      font-size: 0.8rem;
      border-bottom: 1px solid var(--glass-border);
    }
    
    .cron-item:last-child { border: none; }
    
    .cron-schedule { color: var(--text3); font-family: monospace; }
    
    /* Sidebar */
    .sidebar { grid-row: span 3; }
    
    /* Quick Stats */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.75rem;
      margin-bottom: 1rem;
    }
    
    .stat-card {
      padding: 1rem;
      text-align: center;
      border-radius: 12px;
      background: rgba(255,255,255,0.03);
    }
    
    .stat-value {
      font-size: 1.5rem;
      font-weight: 700;
      color: var(--accent);
    }
    
    .stat-label {
      font-size: 0.7rem;
      color: var(--text3);
      text-transform: uppercase;
    }
    
    /* Actions */
    .actions {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
      margin-top: 1rem;
    }
    
    .action-btn {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.75rem 1rem;
      background: rgba(102,126,234,0.1);
      border: 1px solid rgba(102,126,234,0.2);
      border-radius: 10px;
      color: var(--text);
      font-size: 0.85rem;
      cursor: pointer;
      transition: all 0.2s;
    }
    
    .action-btn:hover {
      background: rgba(102,126,234,0.2);
      border-color: rgba(102,126,234,0.4);
    }
    
    /* Empty State */
    .empty {
      text-align: center;
      padding: 2rem;
      color: var(--text3);
      font-size: 0.85rem;
    }
    
    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--glass-border); border-radius: 3px; }
  </style>
</head>
<body>
  <div class="bg-effects"></div>
  
  <header class="header">
    <div class="logo">
      <span>⚡</span> Robothor Dashboard
    </div>
    <div class="header-right">
      <div class="live-indicator">
        <span class="live-dot"></span>
        <span>Live</span>
      </div>
      <div class="time" id="clock"></div>
    </div>
  </header>
  
  <main class="main">
    <!-- Email Panel -->
    <div class="panel glass" id="email-panel">
      <div class="panel-header">
        <div class="panel-title">📧 Email Queue</div>
        <span class="panel-badge" id="email-badge">0</span>
      </div>
      <div class="panel-content" id="email-list"></div>
    </div>
    
    <!-- Calendar Panel -->
    <div class="panel glass" id="calendar-panel">
      <div class="panel-header">
        <div class="panel-title">📅 Today's Schedule</div>
        <span class="panel-badge success" id="calendar-badge">0</span>
      </div>
      <div class="panel-content" id="calendar-list"></div>
    </div>
    
    <!-- Sidebar -->
    <div class="sidebar">
      <!-- Services -->
      <div class="panel glass" style="margin-bottom: 1rem;">
        <div class="panel-header">
          <div class="panel-title">🔌 Services</div>
        </div>
        <div class="services-grid" id="services-list"></div>
      </div>
      
      <!-- Quick Stats -->
      <div class="panel glass" style="margin-bottom: 1rem;">
        <div class="panel-header">
          <div class="panel-title">📊 Quick Stats</div>
        </div>
        <div class="stats-grid" id="stats-grid"></div>
      </div>
      
      <!-- Actions -->
      <div class="panel glass">
        <div class="panel-header">
          <div class="panel-title">⚡ Quick Actions</div>
        </div>
        <div class="actions">
          <button class="action-btn" onclick="triggerBriefing()">🌅 Morning Briefing</button>
          <button class="action-btn" onclick="refreshData()">🔄 Refresh Data</button>
          <button class="action-btn" onclick="window.open('https://mail.google.com', '_blank')">📬 Open Gmail</button>
          <button class="action-btn" onclick="window.open('https://calendar.google.com', '_blank')">📆 Open Calendar</button>
        </div>
      </div>
    </div>
    
    <!-- Tasks Panel -->
    <div class="panel glass">
      <div class="panel-header">
        <div class="panel-title">📋 Tasks</div>
        <span class="panel-badge warning" id="tasks-badge">0</span>
      </div>
      <div class="panel-content" id="tasks-list"></div>
    </div>
    
    <!-- Crons Panel -->
    <div class="panel glass">
      <div class="panel-header">
        <div class="panel-title">⏰ System Crons</div>
      </div>
      <div class="panel-content" id="crons-list"></div>
    </div>
  </main>
  
  <script>
    let data = null;
    
    function updateClock() {
      const now = new Date();
      document.getElementById('clock').textContent = now.toLocaleTimeString('en-US', {
        hour: 'numeric',
        minute: '2-digit',
        timeZone: 'America/New_York'
      }) + ' EST';
    }
    
    function formatTime(isoString) {
      if (!isoString) return '';
      const d = new Date(isoString);
      return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    }
    
    function formatDate(isoString) {
      if (!isoString) return '';
      const d = new Date(isoString);
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }
    
    function renderEmails(emails) {
      const list = document.getElementById('email-list');
      const badge = document.getElementById('email-badge');
      
      badge.textContent = emails.stats.unread || 0;
      badge.className = 'panel-badge' + (emails.stats.urgent > 0 ? ' danger' : '');
      
      if (!emails.emails.length) {
        list.innerHTML = '<div class="empty">No emails to display</div>';
        return;
      }
      
      list.innerHTML = emails.emails.slice(0, 10).map(e => {
        const isUnread = !e.reviewedAt;
        const isUrgent = e.urgency === 'high' || e.urgency === 'critical';
        const from = (e.from || 'Unknown').replace(/<.*>/, '').trim().slice(0, 30);
        
        return \`<div class="email-item \${isUnread ? 'unread' : ''} \${isUrgent ? 'urgent' : ''}">
          <div class="email-from">\${from}</div>
          <div class="email-subject">\${e.subject || '(no subject)'}</div>
          <div class="email-meta">
            <span>\${e.urgency || 'low'}</span>
            <span>\${formatDate(e.receivedAt)}</span>
          </div>
        </div>\`;
      }).join('');
    }
    
    function renderCalendar(calendar) {
      const list = document.getElementById('calendar-list');
      const badge = document.getElementById('calendar-badge');
      
      badge.textContent = calendar.meetings.length;
      
      if (!calendar.meetings.length) {
        list.innerHTML = '<div class="empty">No meetings today</div>';
        return;
      }
      
      const now = new Date();
      
      list.innerHTML = calendar.meetings.map(m => {
        const start = new Date(m.start);
        const diff = (start - now) / 60000;
        const isSoon = diff > 0 && diff <= 30;
        const attendeeCount = (m.attendees || []).length;
        
        return \`<div class="meeting-item \${isSoon ? 'meeting-soon' : ''}">
          <div class="meeting-time">\${formatTime(m.start)}</div>
          <div class="meeting-info">
            <div class="meeting-title">\${m.title}</div>
            <div class="meeting-attendees">\${attendeeCount} attendees</div>
          </div>
        </div>\`;
      }).join('');
    }
    
    function renderTasks(tasks) {
      const list = document.getElementById('tasks-list');
      const badge = document.getElementById('tasks-badge');
      
      badge.textContent = tasks.stats.pending + tasks.stats.inProgress;
      
      const allTasks = [...tasks.pending, ...tasks.inProgress].slice(0, 10);
      
      if (!allTasks.length) {
        list.innerHTML = '<div class="empty">No open tasks</div>';
        return;
      }
      
      list.innerHTML = allTasks.map(t => \`
        <div class="task-item">
          <div class="task-status \${t.status}"></div>
          <div>
            <div class="task-text">\${t.description?.slice(0, 60) || t.id}</div>
            <div class="task-meta">\${t.owner || ''} · \${t.source || ''}</div>
          </div>
        </div>
      \`).join('');
    }
    
    function renderServices(services) {
      const list = document.getElementById('services-list');
      
      list.innerHTML = services.map(s => \`
        <div class="service-item">
          <div class="service-dot \${s.status}"></div>
          <span>\${s.name}</span>
        </div>
      \`).join('');
    }
    
    function renderStats(data) {
      const grid = document.getElementById('stats-grid');
      
      grid.innerHTML = \`
        <div class="stat-card">
          <div class="stat-value">\${data.emails.stats.unread || 0}</div>
          <div class="stat-label">Unread</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">\${data.calendar.meetings.length}</div>
          <div class="stat-label">Meetings</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">\${data.tasks.stats.pending || 0}</div>
          <div class="stat-label">Tasks</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">\${data.security.unreviewed || 0}</div>
          <div class="stat-label">Alerts</div>
        </div>
      \`;
    }
    
    function renderCrons(crons) {
      const list = document.getElementById('crons-list');
      
      if (!crons.systemCrons.length) {
        list.innerHTML = '<div class="empty">No crons found</div>';
        return;
      }
      
      list.innerHTML = crons.systemCrons.slice(0, 6).map(c => \`
        <div class="cron-item">
          <span>\${c.command}</span>
          <span class="cron-schedule">\${c.schedule}</span>
        </div>
      \`).join('');
    }
    
    function render(data) {
      renderEmails(data.emails);
      renderCalendar(data.calendar);
      renderTasks(data.tasks);
      renderServices(data.services);
      renderStats(data);
      renderCrons(data.crons);
    }
    
    async function fetchData() {
      try {
        const res = await fetch('/api/data');
        data = await res.json();
        render(data);
      } catch (e) {
        console.error('Failed to fetch data:', e);
      }
    }
    
    function refreshData() {
      fetchData();
    }
    
    function triggerBriefing() {
      alert('Briefing trigger not yet implemented - would send event via Agent Engine');
    }
    
    // Initialize
    updateClock();
    setInterval(updateClock, 1000);
    fetchData();
    setInterval(fetchData, 30000); // Refresh every 30 seconds
  </script>
</body>
</html>`;
}

// ============ HTTP Server ============

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  
  if (url.pathname === '/' || url.pathname === '/index.html') {
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(renderDashboard());
  } else if (url.pathname === '/api/data') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(getAllData()));
  } else if (url.pathname === '/api/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', timestamp: Date.now() }));
  } else {
    res.writeHead(404);
    res.end('Not found');
  }
});

server.listen(PORT, () => {
  console.log(`Robothor Dashboard at http://localhost:${PORT}`);
});
