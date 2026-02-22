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

async function bridgeCall(
  method: string,
  path: string,
  body?: Record<string, unknown>,
  agentId?: string,
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

export async function create_task(data: { title: string; body?: string; dueAt?: string; personId?: string }) {
  return bridgeCall("POST", "/api/tasks", data);
}

export async function list_tasks({ status, personId }: { status?: string; personId?: string } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (personId) params.set("personId", personId);
  const qs = params.toString();
  return bridgeCall("GET", `/api/tasks${qs ? `?${qs}` : ""}`);
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
  return bridgeCall("PATCH", `/api/conversations/${data.conversationId}`, { status: data.status });
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

// ── Health ──────────────────────────────────────────────────────────────────

export async function crm_health() {
  return bridgeCall("GET", "/health");
}
