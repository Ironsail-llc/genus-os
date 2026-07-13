/**
 * Session Persistence API — Save and restore Helm dashboard state.
 *
 * GET  /api/session — Restore last saved dashboard HTML
 * POST /api/session — Save current dashboard HTML
 *
 * Uses the agent_memory_blocks table (block: "helm_state") for persistence.
 */

import { NextRequest, NextResponse } from "next/server";
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const BLOCK_NAME = "helm_state";
const MAX_DASHBOARD_SIZE = 100_000; // 100KB limit

/**
 * Build a fetch target against the trusted bridge backend and assert the
 * resolved origin matches the configured backend origin. This breaks any
 * taint flow into the outbound fetch and prevents SSRF via path escape.
 * Returns null if the resolved origin differs from the trusted backend.
 */
function safeBackendUrl(base: string, path: string): URL | null {
  const baseUrl = new URL(base);
  const target = new URL(
    path.replace(/^\/+/, ""),
    baseUrl.origin + baseUrl.pathname.replace(/\/?$/, "/")
  );
  if (target.origin !== baseUrl.origin) {
    return null;
  }
  return target;
}

/**
 * GET /api/session — Restore saved dashboard state
 */
export async function GET() {
  try {
    const target = safeBackendUrl(BRIDGE_URL, `api/memory-blocks/${BLOCK_NAME}`);
    if (!target) {
      return new Response("Bad gateway path", { status: 502 });
    }
    const res = await fetch(
      target.toString(),
      { headers: await bridgeAuthHeaders() }
    );

    if (!res.ok) {
      if (res.status === 404) {
        return NextResponse.json({ html: null });
      }
      return NextResponse.json({ error: "Session backend unavailable" }, { status: 502 });
    }

    const data = await res.json();
    const content = data.content || data.block?.content || null;

    if (!content) {
      return NextResponse.json({ html: null });
    }

    try {
      const state = JSON.parse(content);
      return NextResponse.json({
        html: state.html || null,
        savedAt: state.savedAt || null,
      });
    } catch {
      // Content is not JSON — treat as raw HTML (backward compat)
      return NextResponse.json({ html: content });
    }
  } catch {
    return NextResponse.json({ error: "Session backend unavailable" }, { status: 502 });
  }
}

/**
 * POST /api/session — Save dashboard state
 */
export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { html } = body as { html?: string };

    if (!html || typeof html !== "string") {
      return NextResponse.json({ error: "Missing 'html' field" }, { status: 400 });
    }

    if (html.length > MAX_DASHBOARD_SIZE) {
      return NextResponse.json(
        { error: `Dashboard too large (${html.length} > ${MAX_DASHBOARD_SIZE})` },
        { status: 413 }
      );
    }

    const state = JSON.stringify({
      html,
      savedAt: new Date().toISOString(),
    });

    const target = safeBackendUrl(BRIDGE_URL, `api/memory-blocks/${BLOCK_NAME}`);
    if (!target) {
      return new Response("Bad gateway path", { status: 502 });
    }
    const res = await fetch(
      target.toString(),
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          ...(await bridgeAuthHeaders()),
        },
        body: JSON.stringify({ content: state }),
      }
    );

    if (!res.ok) {
      return NextResponse.json({ error: "Session backend unavailable" }, { status: 502 });
    }

    return NextResponse.json({ success: true });
  } catch {
    return NextResponse.json({ error: "Session backend unavailable" }, { status: 502 });
  }
}
