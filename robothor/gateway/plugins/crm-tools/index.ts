/**
 * CRM Tools — OpenClaw plugin template
 *
 * Provides CRM, memory, and pipeline tools to OpenClaw agents via the Bridge HTTP API.
 * The Bridge service (default port 9100) proxies requests to the robothor Python package.
 *
 * Usage:
 *   1. Copy this directory to ~/.openclaw/extensions/crm-tools/
 *   2. Set BRIDGE_URL env var (default: http://localhost:9100)
 *   3. Add "crm-tools" to plugins.allow in openclaw.json
 *   4. Restart the OpenClaw gateway
 *
 * Each tool function calls the Bridge REST API. The Bridge routes to robothor.* Python modules.
 */

const BRIDGE_URL = process.env.BRIDGE_URL || "http://localhost:9100";

// ── Helper ───────────────────────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function bridgeCall(
  method: string,
  path: string,
  body?: Record<string, unknown>,
  agentId?: string,
  _retries: number = 0,
): Promise<unknown> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (agentId) headers["X-Agent-Id"] = agentId;

  const res = await fetch(`${BRIDGE_URL}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text();

    // Retry once on 5xx (transient server errors)
    if (res.status >= 500 && _retries < 1) {
      console.error(`[crm-tools] ${method} ${path} returned ${res.status}, retrying in 2s`);
      await sleep(2000);
      return bridgeCall(method, path, body, agentId, _retries + 1);
    }

    // Return structured error for 4xx so agents see clear failure messages
    if (res.status >= 400 && res.status < 500) {
      return { error: true, status: res.status, message: text.slice(0, 500) };
    }

    throw new Error(`Bridge ${method} ${path} failed (${res.status}): ${text}`);
  }

  return res.json();
}

// ── CRM: People ──────────────────────────────────────────────────────────────

export async function list_people({ search, limit }: { search?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  return bridgeCall("GET", `/api/people${qs ? `?${qs}` : ""}`);
}

export async function get_person({ id }: { id: string }) {
  return bridgeCall("GET", `/api/people/${id}`);
}

export async function create_person(data: {
  firstName: string;
  lastName?: string;
  email?: string;
  phone?: string;
}) {
  return bridgeCall("POST", "/api/people", data);
}

export async function update_person({ id, ...data }: { id: string; [key: string]: unknown }) {
  return bridgeCall("PATCH", `/api/people/${id}`, data);
}

export async function delete_person({ id }: { id: string }) {
  return bridgeCall("DELETE", `/api/people/${id}`);
}

// ── CRM: Companies ───────────────────────────────────────────────────────────

export async function list_companies({ search, limit }: { search?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  return bridgeCall("GET", `/api/companies${qs ? `?${qs}` : ""}`);
}

export async function create_company(data: { name: string; domainName?: string }) {
  return bridgeCall("POST", "/api/companies", data);
}

// ── CRM: Notes & Tasks ──────────────────────────────────────────────────────

export async function create_note(data: { title: string; body: string; personId?: string; companyId?: string }) {
  return bridgeCall("POST", "/api/notes", data);
}

export async function list_notes({ personId, companyId }: { personId?: string; companyId?: string } = {}) {
  const params = new URLSearchParams();
  if (personId) params.set("personId", personId);
  if (companyId) params.set("companyId", companyId);
  const qs = params.toString();
  return bridgeCall("GET", `/api/notes${qs ? `?${qs}` : ""}`);
}

export async function create_task(data: { title: string; body?: string; dueAt?: string; personId?: string; [key: string]: unknown }) {
  const result = await bridgeCall("POST", "/api/tasks", data) as Record<string, unknown>;
  if (result && typeof result === "object" && "error" in result) {
    return result; // Pass structured error through to agent
  }
  if (!result || typeof result !== "object" || !("id" in result)) {
    return { error: true, status: 0, message: "create_task: response missing 'id' field" };
  }
  return result;
}

export async function list_tasks({
  status,
  personId,
  assignedToAgent,
  createdByAgent,
  tags,
  priority,
  excludeResolved,
  limit,
}: {
  status?: string;
  personId?: string;
  assignedToAgent?: string;
  createdByAgent?: string;
  tags?: string[];
  priority?: string;
  excludeResolved?: boolean;
  limit?: number;
} = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (personId) params.set("personId", personId);
  if (assignedToAgent) params.set("assignedToAgent", assignedToAgent);
  if (createdByAgent) params.set("createdByAgent", createdByAgent);
  if (tags && tags.length > 0) params.set("tags", tags.join(","));
  if (priority) params.set("priority", priority);
  if (excludeResolved) params.set("excludeResolved", "true");
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  return bridgeCall("GET", `/api/tasks${qs ? `?${qs}` : ""}`);
}

export async function get_task({ taskId }: { taskId: string }) {
  return bridgeCall("GET", `/api/tasks/${taskId}`);
}

export async function update_task({ taskId, ...data }: { taskId: string; [key: string]: unknown }) {
  return bridgeCall("PATCH", `/api/tasks/${taskId}`, data);
}

export async function delete_task({ taskId }: { taskId: string }) {
  return bridgeCall("DELETE", `/api/tasks/${taskId}`);
}

// ── CRM: Conversations ──────────────────────────────────────────────────────

export async function list_conversations({ status }: { status?: string } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  const qs = params.toString();
  return bridgeCall("GET", `/api/conversations${qs ? `?${qs}` : ""}`);
}

export async function get_conversation({ conversationId }: { conversationId: number }) {
  return bridgeCall("GET", `/api/conversations/${conversationId}`);
}

export async function list_messages({ conversationId }: { conversationId: number }) {
  return bridgeCall("GET", `/api/conversations/${conversationId}/messages`);
}

export async function create_message(data: { conversationId: number; content: string; messageType?: string }) {
  return bridgeCall("POST", `/api/conversations/${data.conversationId}/messages`, data);
}

export async function toggle_conversation_status(data: { conversationId: number; status: string }) {
  return bridgeCall("POST", `/api/conversations/${data.conversationId}/toggle_status`, { status: data.status });
}

// ── CRM: Merge ──────────────────────────────────────────────────────────────

export async function merge_contacts(data: { keeperId: string; loserId: string }) {
  return bridgeCall("POST", "/api/people/merge", data);
}

export async function merge_companies(data: { keeperId: string; loserId: string }) {
  return bridgeCall("POST", "/api/companies/merge", data);
}

// ── CRM: Interaction Logging ────────────────────────────────────────────────

export async function log_interaction(data: {
  contact_name: string;
  channel: string;
  direction: "incoming" | "outgoing";
  content_summary: string;
  channel_identifier?: string;
}) {
  return bridgeCall("POST", "/api/interactions", data);
}

// ── Memory ──────────────────────────────────────────────────────────────────

export async function search_memory({ query, limit }: { query: string; limit?: number }) {
  return bridgeCall("POST", "/api/memory/search", { query, limit });
}

export async function store_memory({ content, content_type }: { content: string; content_type?: string }) {
  return bridgeCall("POST", "/api/memory/store", { content, content_type });
}

export async function get_entity({ name }: { name: string }) {
  return bridgeCall("GET", `/api/memory/entity/${encodeURIComponent(name)}`);
}

export async function memory_stats() {
  return bridgeCall("GET", "/api/memory/stats");
}

export async function memory_block_read({ block_name }: { block_name: string }) {
  return bridgeCall("GET", `/api/memory/blocks/${encodeURIComponent(block_name)}`);
}

export async function memory_block_write({ block_name, content }: { block_name: string; content: string }) {
  return bridgeCall("PUT", `/api/memory/blocks/${encodeURIComponent(block_name)}`, { content });
}

// ── Pipeline ────────────────────────────────────────────────────────────────

export async function pipeline_status() {
  return bridgeCall("GET", "/api/pipeline/status");
}

export async function pipeline_trigger({ tier }: { tier: number }) {
  return bridgeCall("POST", `/api/pipeline/trigger/${tier}`);
}

// ── Task Coordination: Resolve + Agent Inbox ────────────────────────────────

export async function resolve_task(data: { taskId: string; resolution: string }) {
  return bridgeCall("POST", `/api/tasks/${data.taskId}/resolve`, { resolution: data.resolution });
}

export async function list_my_tasks({
  agentId,
  status,
  includeUnassigned,
  limit,
}: {
  agentId: string;
  status?: string;
  includeUnassigned?: boolean;
  limit?: number;
}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (includeUnassigned) params.set("includeUnassigned", "true");
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  return bridgeCall("GET", `/api/tasks/agent/${agentId}${qs ? `?${qs}` : ""}`);
}

// ── Task Review Workflow ────────────────────────────────────────────────────

export async function approve_task(
  data: { taskId: string; resolution?: string },
  agentId?: string,
) {
  return bridgeCall(
    "POST",
    `/api/tasks/${data.taskId}/approve`,
    { resolution: data.resolution || "Approved" },
    agentId,
  );
}

export async function reject_task(
  data: { taskId: string; reason: string; changeRequests?: string[] },
  agentId?: string,
) {
  return bridgeCall(
    "POST",
    `/api/tasks/${data.taskId}/reject`,
    { reason: data.reason, changeRequests: data.changeRequests },
    agentId,
  );
}

// ── Agent Notifications ─────────────────────────────────────────────────────

export async function send_notification(data: {
  fromAgent: string;
  toAgent: string;
  notificationType: string;
  subject: string;
  body?: string;
  metadata?: Record<string, unknown>;
  taskId?: string;
}) {
  return bridgeCall("POST", "/api/notifications/send", data);
}

export async function get_inbox({
  agentId,
  unreadOnly,
  typeFilter,
  limit,
}: {
  agentId: string;
  unreadOnly?: boolean;
  typeFilter?: string;
  limit?: number;
}) {
  const params = new URLSearchParams();
  if (unreadOnly !== undefined) params.set("unreadOnly", String(unreadOnly));
  if (typeFilter) params.set("typeFilter", typeFilter);
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  return bridgeCall("GET", `/api/notifications/inbox/${agentId}${qs ? `?${qs}` : ""}`);
}

export async function ack_notification({ notificationId }: { notificationId: string }) {
  return bridgeCall("POST", `/api/notifications/${notificationId}/ack`);
}

// ── Shared Working State ────────────────────────────────────────────────────

export async function append_to_block(data: {
  block_name: string;
  entry: string;
  maxEntries?: number;
}) {
  return bridgeCall("POST", `/api/memory/blocks/${encodeURIComponent(data.block_name)}/append`, {
    entry: data.entry,
    maxEntries: data.maxEntries,
  });
}

// ── Health ──────────────────────────────────────────────────────────────────

export async function crm_health() {
  return bridgeCall("GET", "/health");
}
