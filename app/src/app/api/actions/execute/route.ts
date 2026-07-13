/**
 * Action Execution API — Proxies dashboard actions to Bridge (:9100).
 *
 * POST /api/actions/execute
 * Body: { tool: string, params: Record<string, unknown> }
 *
 * Forwards the signed-in user's bridge token (Authorization: Bearer) for RBAC
 * enforcement at Bridge. A legacy agent header exists only in the explicit
 * loopback-only insecure development mode. Rate-limited to 10 actions per
 * minute per apparent client IP; production ingress must overwrite forwarded
 * IP headers and apply a distributed limit as defense in depth.
 */

import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";

// Simple in-memory rate limiter (10 actions/minute)
const rateLimiter = new Map<string, { count: number; resetAt: number }>();
const RATE_LIMIT = 10;
const RATE_WINDOW_MS = 60_000;
let lastCleanup = Date.now();

class ActionInputError extends Error {}

const ActionParamsSchema = z.record(z.string(), z.unknown());
const FORBIDDEN_PARAM_KEYS = new Set(["__proto__", "prototype", "constructor"]);

function assertSafeParameterTree(value: unknown, depth = 0): void {
  if (!value || typeof value !== "object") return;
  if (depth > 32) throw new ActionInputError("Invalid action parameters");

  if (!Array.isArray(value)) {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new ActionInputError("Invalid action parameters");
    }
  }

  for (const key of Object.keys(value)) {
    if (FORBIDDEN_PARAM_KEYS.has(key)) {
      throw new ActionInputError("Invalid action parameters");
    }
    assertSafeParameterTree((value as Record<string, unknown>)[key], depth + 1);
  }
}

function actionParams(value: unknown): Record<string, unknown> {
  if (value === undefined) return {};
  assertSafeParameterTree(value);
  const parsed = ActionParamsSchema.parse(value);
  if (Buffer.byteLength(JSON.stringify(parsed), "utf8") > 65_536) {
    throw new ActionInputError("Invalid action parameters");
  }
  return parsed;
}

function resourceId(params: Record<string, unknown>, key: string): string {
  const value = params[key];
  if (typeof value !== "string" && typeof value !== "number") {
    throw new ActionInputError(`Missing or invalid '${key}'`);
  }
  const id = String(value);
  if (
    id.length > 128 ||
    id.includes("..") ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(id)
  ) {
    throw new ActionInputError(`Invalid '${key}'`);
  }
  return encodeURIComponent(id);
}

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
    path: (p) => {
      const query = new URLSearchParams({
        status: String(p.status || "open"),
        page: String(p.page || 1),
      });
      return `/api/conversations?${query.toString()}`;
    },
  },
  get_conversation: {
    method: "GET",
    path: (p) => `/api/conversations/${resourceId(p, "conversation_id")}`,
  },
  list_messages: {
    method: "GET",
    path: (p) => `/api/conversations/${resourceId(p, "conversation_id")}/messages`,
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
    path: (p) => `/api/conversations/${resourceId(p, "conversation_id")}/messages`,
    bodyKeys: ["content", "message_type", "private"],
  },
  toggle_conversation_status: {
    method: "POST",
    path: (p) => `/api/conversations/${resourceId(p, "conversation_id")}/toggle_status`,
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
    path: (p) => `/api/tasks/${resourceId(p, "task_id")}`,
    bodyKeys: ["status", "priority", "resolution", "assignedToAgent", "tags"],
  },
  resolve_task: {
    method: "POST",
    path: (p) => `/api/tasks/${resourceId(p, "task_id")}/resolve`,
    bodyKeys: ["resolution"],
  },
  get_task_history: {
    method: "GET",
    path: (p) => `/api/tasks/${resourceId(p, "task_id")}/history`,
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
    path: (p) => `/api/routines/${resourceId(p, "routine_id")}`,
    bodyKeys: ["title", "body", "cronExpr", "timezone", "assignedToAgent", "priority", "tags", "active"],
  },
  delete_routine: {
    method: "DELETE",
    path: (p) => `/api/routines/${resourceId(p, "routine_id")}`,
  },
  approve_task: {
    method: "POST",
    path: (p) => `/api/tasks/${resourceId(p, "task_id")}/approve`,
    bodyKeys: ["resolution"],
  },
  reject_task: {
    method: "POST",
    path: (p) => `/api/tasks/${resourceId(p, "task_id")}/reject`,
    bodyKeys: ["reason", "changeRequests"],
  },
  answer_question: {
    method: "POST",
    path: (p) => `/api/tasks/${resourceId(p, "task_id")}/answer`,
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
    path: (p) => `/api/notifications/inbox/${resourceId(p, "agent_id")}?unreadOnly=${p.unreadOnly ?? true}&limit=${p.limit || 50}`,
  },
};

export async function POST(request: NextRequest) {
  try {
    // Resolve and verify the caller before any user-controlled value can affect
    // validation, routing, or backend access. Bridge independently verifies the
    // same signed token and its tenant/role/scope claims.
    const authHeaders = await bridgeAuthHeaders();
    if (!authHeaders.Authorization && !authHeaders["X-Agent-Id"]) {
      return NextResponse.json({ error: "authentication required" }, { status: 401 });
    }

    const ip = request.headers.get("x-forwarded-for") || "unknown";
    if (!checkRateLimit(ip)) {
      return NextResponse.json(
        { error: "Rate limit exceeded (10 actions/minute)" },
        { status: 429 }
      );
    }

    const body = await request.json();
    const { tool, params } = body as { tool?: string; params?: Record<string, unknown> };

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

    const resolvedParams = actionParams(params);
    const base = new URL(BRIDGE_URL);
    const pathStr = route.path(resolvedParams);
    const target = new URL(
      pathStr.replace(/^\/+/, ""),
      base.origin + base.pathname.replace(/\/?$/, "/")
    );
    if (target.origin !== base.origin) {
      return NextResponse.json({ error: "Bad gateway path" }, { status: 502 });
    }
    const url = target.toString();
    const headers: Record<string, string> = {
      ...authHeaders,
      "Content-Type": "application/json",
    };
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
      const publicError =
        data && typeof data.error === "string"
          ? data.error.slice(0, 500)
          : `Bridge returned ${response.status}`;
      return NextResponse.json(
        { error: publicError },
        { status: response.status }
      );
    }

    return NextResponse.json({ success: true, data });
  } catch (error) {
    if (error instanceof z.ZodError) {
      return NextResponse.json({ error: "Invalid action parameters" }, { status: 400 });
    }
    if (error instanceof ActionInputError) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    console.error("[action-execute] backend request failed");
    return NextResponse.json(
      { error: "Action service temporarily unavailable" },
      { status: 502 },
    );
  }
}
