/**
 * Session Persistence API — Save and restore Helm dashboard state.
 *
 * GET  /api/session — Restore last saved dashboard HTML
 * POST /api/session — Save current dashboard HTML
 *
 * Uses the agent_memory_blocks table (block: "helm_state") for persistence.
 */

import { NextRequest, NextResponse } from "next/server";
import { getServiceUrl } from "@/lib/services/registry";

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const BLOCK_NAME = "helm_state";
const AGENT_ID = "helm-user";
const MAX_DASHBOARD_SIZE = 100_000; // 100KB limit

/**
 * GET /api/session — Restore saved dashboard state
 */
export async function GET() {
  try {
    const res = await fetch(
      `${BRIDGE_URL}/api/memory-blocks/${BLOCK_NAME}`,
      { headers: { "X-Agent-Id": AGENT_ID } }
    );

    if (!res.ok) {
      if (res.status === 404) {
        return NextResponse.json({ html: null });
      }
      return NextResponse.json({ error: "Failed to read session" }, { status: 500 });
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
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 500 });
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

    const res = await fetch(
      `${BRIDGE_URL}/api/memory-blocks/${BLOCK_NAME}`,
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Id": AGENT_ID,
        },
        body: JSON.stringify({ content: state }),
      }
    );

    if (!res.ok) {
      const text = await res.text();
      return NextResponse.json(
        { error: `Bridge returned ${res.status}: ${text}` },
        { status: 500 }
      );
    }

    return NextResponse.json({ success: true });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
