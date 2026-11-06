/**
 * Session Persistence API — Save and restore Helm dashboard state.
 *
 * GET  /api/session — Restore last saved dashboard HTML
 * POST /api/session — Save current dashboard HTML
 *
 * Uses the agent_memory_blocks table for persistence, one block per signed-in
 * person ("helm_state.<hash>"). Blocks are keyed by tenant, and a canvas is
 * drawn from one person's conversation: a single shared name would show it to
 * everyone in the tenant.
 */

import { createHash } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";
import { bridgeAuthHeaders } from "@/lib/bridge-auth";
import { getServiceUrl } from "@/lib/services/registry";

const BRIDGE_URL = getServiceUrl("bridge") || "http://localhost:9100";
const BLOCK_PREFIX = "helm_state";
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

/** The roles the bridge serves memory blocks to (its privileged-access gate). */
const KEEPS_A_VIEW = new Set(["owner", "admin"]);

/**
 * The signed-in person's own block.
 *
 * - `null`: the session identifies nobody.
 * - `false`: it identifies somebody the bridge keeps no blocks for. Asking
 *   anyway is a refusal and an `auth.denied` audit event on every canvas load,
 *   so the bridge is not asked and the canvas is simply not kept for them.
 *
 * A session carries the person's name, email and role; it has an id only where
 * a deployment's sign-in adds one. An id is used exactly as given, an address
 * without regard to case. Either is hashed into the block name, so two people
 * never share a block and nobody's address is written into a block name, a URL
 * or a log line.
 */
async function ownBlockName(): Promise<string | false | null> {
  const user = (await auth())?.user;
  const identity = user?.id || user?.email?.trim().toLowerCase();
  if (!identity) return null;
  if (!KEEPS_A_VIEW.has(user?.role ?? "")) return false;
  const key = createHash("sha256").update(identity).digest("hex").slice(0, 32);
  return `${BLOCK_PREFIX}.${key}`;
}

const NOT_SIGNED_IN = { error: "Not signed in" };

/**
 * GET /api/session — Restore saved dashboard state
 */
export async function GET() {
  try {
    const BLOCK_NAME = await ownBlockName();
    if (BLOCK_NAME === null) return NextResponse.json(NOT_SIGNED_IN, { status: 401 });
    if (BLOCK_NAME === false) return NextResponse.json({ html: null });
    const target = safeBackendUrl(BRIDGE_URL, `api/memory/blocks/${BLOCK_NAME}`);
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

    const BLOCK_NAME = await ownBlockName();
    if (BLOCK_NAME === null) return NextResponse.json(NOT_SIGNED_IN, { status: 401 });
    if (BLOCK_NAME === false) return NextResponse.json({ success: false, kept: false });

    const state = JSON.stringify({
      html,
      savedAt: new Date().toISOString(),
    });

    const target = safeBackendUrl(BRIDGE_URL, `api/memory/blocks/${BLOCK_NAME}`);
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
