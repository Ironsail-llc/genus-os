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

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const AGENT_ID = "helm-user";

// Simple in-memory rate limiter (10 actions/minute)
const rateLimiter = new Map<string, { count: number; resetAt: number }>();
const RATE_LIMIT = 10;
const RATE_WINDOW_MS = 60_000;

function checkRateLimit(ip: string): boolean {
  const now = Date.now();
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

    const resolvedParams = params || {};
    const url = `${BRIDGE_URL}${route.path(resolvedParams)}`;
    const headers: Record<string, string> = {
      "X-Agent-Id": AGENT_ID,
      "Content-Type": "application/json",
    };

    let response: Response;
    if (route.method === "GET") {
      response = await fetch(url, { headers });
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
        method: route.method,
        headers,
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
