/**
 * Action Execution API — Proxies dashboard actions to Bridge (:9100).
 *
 * POST /api/actions/execute
 * Body: { tool: string, params: Record<string, unknown> }
 *
 * Uses the "helm-user" agent identity for RBAC enforcement at Bridge.
 * Rate-limited to 10 actions per minute per IP.
 */

import { NextRequest, NextResponse } from "next/server";
import { getServiceUrl } from "@/lib/services/registry";
import { HELM_AGENT_ID } from "@/lib/config";

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";

// Simple in-memory rate limiter (10 actions/minute)
const rateLimiter = new Map<string, { count: number; resetAt: number }>();
const RATE_LIMIT = 10;
const RATE_WINDOW_MS = 60_000;
let lastCleanup = Date.now();

function checkRateLimit(ip: string): boolean {
  const now = Date.now();
  // Purge expired entries periodically (every 5 min)
  if (now - lastCleanup > 5 * 60_000) {
    for (const [key, entry] of rateLimiter) {
      if (now > entry.resetAt) rateLimiter.delete(key);
    }
    lastCleanup = now;
  }
  const entry = rateLimiter.get(ip);
  if (!entry || now > entry.resetAt) {
    rateLimiter.set(ip, { count: 1, resetAt: now + RATE_WINDOW_MS });
    return true;
  }
  if (entry.count >= RATE_LIMIT) return false;
  entry.count++;
  return true;
}

/** Map tool names to Bridge HTTP calls */
const TOOL_ROUTES: Record<string, { method: string; path: (p: Record<string, unknown>) => string; bodyKeys?: string[] }> = {
  list_conversations: {
    method: "GET",
    path: (p) => `/api/conversations?status=${p.status || "open"}&page=${p.page || 1}`,
  },
  get_conversation: {
    method: "GET",
    path: (p) => `/api/conversations/${p.conversation_id}`,
  },
  list_messages: {
    method: "GET",
    path: (p) => `/api/conversations/${p.conversation_id}/messages`,
  },
  list_people: {
    method: "GET",
    path: (p) => `/api/people?limit=${p.limit || 20}${p.search ? `&search=${encodeURIComponent(String(p.search))}` : ""}`,
  },
  crm_health: {
    method: "GET",
    path: () => "/health",
  },
  create_note: {
    method: "POST",
    path: () => "/api/notes",
    bodyKeys: ["title", "body", "personId", "companyId"],
  },
  create_message: {
    method: "POST",
    path: (p) => `/api/conversations/${p.conversation_id}/messages`,
    bodyKeys: ["content", "message_type", "private"],
  },
  toggle_conversation_status: {
    method: "POST",
    path: (p) => `/api/conversations/${p.conversation_id}/toggle_status`,
    bodyKeys: ["status"],
  },
  log_interaction: {
    method: "POST",
    path: () => "/log-interaction",
    bodyKeys: ["contact_name", "channel", "direction", "content_summary", "channel_identifier"],
  },
  list_tasks: {
    method: "GET",
    path: (p) => {
      const params = new URLSearchParams();
      if (p.status) params.set("status", String(p.status));
      if (p.assignedToAgent) params.set("assignedToAgent", String(p.assignedToAgent));
      if (p.priority) params.set("priority", String(p.priority));
      if (p.tags) params.set("tags", String(p.tags));
      if (p.excludeResolved) params.set("excludeResolved", String(p.excludeResolved));
      params.set("limit", String(p.limit || 100));
      return `/api/tasks?${params.toString()}`;
    },
  },
  update_task: {
    method: "PATCH",
    path: (p) => `/api/tasks/${p.task_id}`,
    bodyKeys: ["status", "priority", "resolution", "assignedToAgent", "tags"],
  },
  resolve_task: {
    method: "POST",
    path: (p) => `/api/tasks/${p.task_id}/resolve`,
    bodyKeys: ["resolution"],
  },
  get_task_history: {
    method: "GET",
    path: (p) => `/api/tasks/${p.task_id}/history`,
  },
  agent_status: {
    method: "GET",
    path: () => "/api/agents/status",
  },
  list_routines: {
    method: "GET",
    path: (p) => `/api/routines?activeOnly=${p.activeOnly ?? true}&limit=${p.limit || 50}`,
  },
  create_routine: {
    method: "POST",
    path: () => "/api/routines",
    bodyKeys: ["title", "cronExpr", "body", "timezone", "assignedToAgent", "priority", "tags"],
  },
  update_routine: {
    method: "PATCH",
    path: (p) => `/api/routines/${p.routine_id}`,
    bodyKeys: ["title", "body", "cronExpr", "timezone", "assignedToAgent", "priority", "tags", "active"],
  },
  delete_routine: {
    method: "DELETE",
    path: (p) => `/api/routines/${p.routine_id}`,
  },
  approve_task: {
    method: "POST",
    path: (p) => `/api/tasks/${p.task_id}/approve`,
    bodyKeys: ["resolution"],
  },
  reject_task: {
    method: "POST",
    path: (p) => `/api/tasks/${p.task_id}/reject`,
    bodyKeys: ["reason", "changeRequests"],
  },
  answer_question: {
    method: "POST",
    path: (p) => `/api/tasks/${p.task_id}/answer`,
    bodyKeys: ["answer", "advanceTo", "channel"],
  },
  list_tenants: {
    method: "GET",
    path: (p) => `/api/tenants?activeOnly=${p.activeOnly ?? true}`,
  },
  send_notification: {
    method: "POST",
    path: () => "/api/notifications/send",
    bodyKeys: ["fromAgent", "toAgent", "notificationType", "subject", "body", "taskId"],
  },
  get_inbox: {
    method: "GET",
    path: (p) => `/api/notifications/inbox/${p.agent_id}?unreadOnly=${p.unreadOnly ?? true}&limit=${p.limit || 50}`,
  },
};

export async function POST(request: NextRequest) {
  try {
    const ip = request.headers.get("x-forwarded-for") || "unknown";
    if (!checkRateLimit(ip)) {
      return NextResponse.json(
        { error: "Rate limit exceeded (10 actions/minute)" },
        { status: 429 }
      );
    }

    const body = await request.json();
    const { tool, params, tenantId } = body as { tool?: string; params?: Record<string, unknown>; tenantId?: string };

    if (!tool || typeof tool !== "string") {
      return NextResponse.json({ error: "Missing 'tool' field" }, { status: 400 });
    }

    const route = TOOL_ROUTES[tool];
    if (!route) {
      return NextResponse.json(
        { error: `Unknown tool '${tool}'` },
        { status: 400 }
      );
    }

    const resolvedParams = params || {};
    const url = `${BRIDGE_URL}${route.path(resolvedParams)}`;
    const headers: Record<string, string> = {
      "X-Agent-Id": HELM_AGENT_ID,
      "Content-Type": "application/json",
    };
    if (tenantId) {
      headers["X-Tenant-Id"] = tenantId;
    }

    let response: Response;
    const fetchOpts = { headers, signal: AbortSignal.timeout(10_000) };
    if (route.method === "GET") {
      response = await fetch(url, fetchOpts);
    } else {
      const requestBody: Record<string, unknown> = {};
      if (route.bodyKeys) {
        for (const key of route.bodyKeys) {
          if (resolvedParams[key] !== undefined) {
            requestBody[key] = resolvedParams[key];
          }
        }
      }
      response = await fetch(url, {
        ...fetchOpts,
        method: route.method,
        body: JSON.stringify(requestBody),
      });
    }

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(
        { error: data.error || `Bridge returned ${response.status}`, data },
        { status: response.status }
      );
    }

    return NextResponse.json({ success: true, data });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
