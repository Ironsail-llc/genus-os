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
    if (!res.ok && res.status !== 409) {
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
