/**
 * HTTP client for the Agent Engine chat endpoints.
 * Server-side only — used by Next.js API routes.
 */
import type { ChatMessage } from "./types";
import { bridgeAuthHeaders } from "@/lib/bridge-auth";

const ENGINE_URL = process.env.ROBOTHOR_ENGINE_URL || "http://127.0.0.1:18800";

async function engineHeaders(json = false): Promise<Record<string, string>> {
  return {
    ...(await bridgeAuthHeaders()),
    ...(json ? { "Content-Type": "application/json" } : {}),
  };
}

async function verifiedDashboardHeaders(): Promise<Record<string, string>> {
  const headers = await engineHeaders(true);
  if (!headers.Authorization?.startsWith("Bearer ")) {
    throw new Error("Dashboard authentication required");
  }
  return headers;
}

class EngineClient {
  /**
   * Run a provider-neutral dashboard completion inside the authenticated
   * Engine. Model routing and provider credentials never enter Next.js.
   */
  async dashboardCompletion(
    purpose: "triage" | "render",
    systemPrompt: string,
    userPrompt: string,
  ): Promise<string> {
    const res = await fetch(`${ENGINE_URL}/api/dashboard/completions`, {
      method: "POST",
      headers: await verifiedDashboardHeaders(),
      body: JSON.stringify({
        purpose,
        system_prompt: systemPrompt,
        user_prompt: userPrompt,
      }),
      signal: AbortSignal.timeout(purpose === "triage" ? 20_000 : 130_000),
    });
    if (!res.ok) {
      // Never copy provider/Engine response text into a dashboard exception.
      throw new Error("Dashboard completion unavailable");
    }
    const payload: unknown = await res.json();
    if (
      !payload ||
      typeof payload !== "object" ||
      !("content" in payload) ||
      typeof payload.content !== "string" ||
      payload.content.length === 0
    ) {
      throw new Error("Dashboard completion unavailable");
    }
    return payload.content;
  }

  /**
   * Send a chat message. Returns the raw Response with SSE body.
   * Caller is responsible for reading the SSE stream.
   */
  async chatSend(sessionKey: string, message: string): Promise<Response> {
    const res = await fetch(`${ENGINE_URL}/chat/send`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey, message }),
      signal: AbortSignal.timeout(120_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res;
  }

  /** Get conversation history for a session. */
  async chatHistory(
    sessionKey: string,
    limit = 50
  ): Promise<{ sessionKey: string; messages: ChatMessage[] }> {
    const res = await fetch(
      `${ENGINE_URL}/chat/history?session_key=${encodeURIComponent(sessionKey)}&limit=${limit}`,
      { headers: await engineHeaders(), signal: AbortSignal.timeout(30_000) },
    );
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  /** Inject a system message into a session. */
  async chatInject(
    sessionKey: string,
    message: string,
    label?: string
  ): Promise<{ ok: boolean }> {
    const res = await fetch(`${ENGINE_URL}/chat/inject`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey, message, label }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  /** Cancel the running response for a session. */
  async chatAbort(sessionKey: string): Promise<{ ok: boolean; aborted: boolean }> {
    const res = await fetch(`${ENGINE_URL}/chat/abort`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  /** Clear session history. */
  async chatClear(sessionKey: string): Promise<{ ok: boolean }> {
    const res = await fetch(`${ENGINE_URL}/chat/clear`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  // ── Plan Mode ──

  /** Start plan mode: explore with read-only tools. Returns SSE stream. */
  async planStart(sessionKey: string, message: string, deepPlan = false): Promise<Response> {
    const res = await fetch(`${ENGINE_URL}/chat/plan/start`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey, message, deep_plan: deepPlan }),
      signal: AbortSignal.timeout(120_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res;
  }

  /** Approve a pending plan. Returns SSE stream of execution. */
  async planApprove(sessionKey: string, planId: string): Promise<Response> {
    const res = await fetch(`${ENGINE_URL}/chat/plan/approve`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey, plan_id: planId }),
      signal: AbortSignal.timeout(120_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res;
  }

  /** Reject a pending plan with optional feedback. */
  async planReject(
    sessionKey: string,
    planId: string,
    feedback?: string
  ): Promise<{ ok: boolean }> {
    const res = await fetch(`${ENGINE_URL}/chat/plan/reject`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey, plan_id: planId, feedback }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  /** Check plan state for a session. */
  async planStatus(
    sessionKey: string
  ): Promise<{ active: boolean; plan?: PlanState }> {
    const res = await fetch(
      `${ENGINE_URL}/chat/plan/status?session_key=${encodeURIComponent(sessionKey)}`,
      { headers: await engineHeaders(), signal: AbortSignal.timeout(30_000) },
    );
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }

  // ── Deep Mode ──

  /** Start deep reasoning. Returns SSE stream. */
  async deepStart(sessionKey: string, query: string): Promise<Response> {
    const res = await fetch(`${ENGINE_URL}/chat/deep/start`, {
      method: "POST",
      headers: await engineHeaders(true),
      body: JSON.stringify({ session_key: sessionKey, query }),
      signal: AbortSignal.timeout(120_000),
    });
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res;
  }

  /** Check deep reasoning state for a session. */
  async deepStatus(
    sessionKey: string
  ): Promise<{ active: boolean; deep?: DeepState }> {
    const res = await fetch(
      `${ENGINE_URL}/chat/deep/status?session_key=${encodeURIComponent(sessionKey)}`,
      { headers: await engineHeaders(), signal: AbortSignal.timeout(30_000) },
    );
    if (!res.ok) {
      throw new Error(`Engine error: ${res.status} ${res.statusText}`);
    }
    return res.json();
  }
}

export interface PlanState {
  plan_id: string;
  plan_text: string;
  original_message: string;
  status: "pending" | "approved" | "rejected" | "expired";
  created_at: string;
  exploration_run_id: string;
  rejection_feedback: string;
}

export interface DeepState {
  deep_id: string;
  query: string;
  status: "running" | "completed" | "failed";
  started_at: string;
  completed_at: string;
  response: string;
  execution_time_s: number;
  cost_usd: number;
  error: string;
}

// Singleton instance
let instance: EngineClient | null = null;

export function getEngineClient(): EngineClient {
  if (!instance) {
    instance = new EngineClient();
  }
  return instance;
}

export { EngineClient };
