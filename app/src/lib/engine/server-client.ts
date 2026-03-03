/**
 * HTTP client for the Agent Engine chat endpoints.
 * Server-side only — used by Next.js API routes.
 */
import type { ChatMessage } from "./types";

const ENGINE_URL = process.env.ROBOTHOR_ENGINE_URL || "http://127.0.0.1:18800";

class EngineClient {
  /**
   * Send a chat message. Returns the raw Response with SSE body.
   * Caller is responsible for reading the SSE stream.
   */
  async chatSend(sessionKey: string, message: string): Promise<Response> {
    const res = await fetch(`${ENGINE_URL}/chat/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey, message }),
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
      `${ENGINE_URL}/chat/history?session_key=${encodeURIComponent(sessionKey)}&limit=${limit}`
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey, message, label }),
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey }),
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey }),
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey, message, deep_plan: deepPlan }),
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey, plan_id: planId }),
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey, plan_id: planId, feedback }),
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
      `${ENGINE_URL}/chat/plan/status?session_key=${encodeURIComponent(sessionKey)}`
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
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_key: sessionKey, query }),
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
      `${ENGINE_URL}/chat/deep/status?session_key=${encodeURIComponent(sessionKey)}`
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
