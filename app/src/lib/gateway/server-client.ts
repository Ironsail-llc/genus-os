/**
 * Singleton WebSocket client for the OpenClaw gateway.
 * Server-side only — used by Next.js API routes.
 */
import WebSocket from "ws";
import { randomUUID } from "crypto";
import type {
  Frame,
  RequestFrame,
  ResponseFrame,
  EventFrame,
  ConnectParams,
  ChatSendParams,
  ChatSendResponse,
  ChatEvent,
  ChatHistoryParams,
  ChatHistoryResponse,
  ChatInjectParams,
  ChatInjectResponse,
  ChatAbortParams,
  ChatAbortResponse,
} from "./types";

const GATEWAY_URL = process.env.OPENCLAW_GATEWAY_URL || "ws://127.0.0.1:18789";
const GATEWAY_TOKEN = () => process.env.OPENCLAW_GATEWAY_TOKEN || "";

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const CONNECT_TIMEOUT_MS = 10000;

interface PendingRequest {
  resolve: (payload: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

type ChatEventListener = (event: ChatEvent) => void;

class GatewayClient {
  private ws: WebSocket | null = null;
  private connected = false;
  private connecting = false;
  private reconnectDelay = RECONNECT_BASE_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pending = new Map<string, PendingRequest>();
  private chatListeners = new Map<string, Set<ChatEventListener>>();
  private tickTimer: ReturnType<typeof setTimeout> | null = null;
  private tickIntervalMs = 30000;
  private destroyed = false;

  async ensureConnected(): Promise<void> {
    if (this.connected) return;
    if (this.connecting) {
      // Wait for the current connection attempt
      return new Promise((resolve, reject) => {
        const check = setInterval(() => {
          if (this.connected) {
            clearInterval(check);
            resolve();
          } else if (!this.connecting) {
            clearInterval(check);
            reject(new Error("Connection failed"));
          }
        }, 100);
        setTimeout(() => {
          clearInterval(check);
          reject(new Error("Connection timeout"));
        }, CONNECT_TIMEOUT_MS);
      });
    }
    return this.connect();
  }

  private connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.destroyed) {
        reject(new Error("Client destroyed"));
        return;
      }
      this.connecting = true;
      const ws = new WebSocket(GATEWAY_URL, {
        headers: { Origin: "http://localhost:18789" },
      });
      this.ws = ws;

      const connectTimeout = setTimeout(() => {
        ws.close();
        this.connecting = false;
        reject(new Error("Connection timeout"));
      }, CONNECT_TIMEOUT_MS);

      ws.on("open", () => {
        // Wait for connect.challenge from server
      });

      ws.on("message", (data) => {
        let frame: Frame;
        try {
          frame = JSON.parse(data.toString());
        } catch {
          return;
        }
        this.handleFrame(frame, resolve, reject, connectTimeout);
      });

      ws.on("close", () => {
        this.onDisconnect();
      });

      ws.on("error", (err) => {
        if (this.connecting) {
          clearTimeout(connectTimeout);
          this.connecting = false;
          reject(err);
        }
      });
    });
  }

  private handleFrame(
    frame: Frame,
    connectResolve?: (value: void) => void,
    connectReject?: (reason: Error) => void,
    connectTimeout?: ReturnType<typeof setTimeout>
  ) {
    if (frame.type === "event") {
      this.handleEvent(frame, connectResolve, connectReject, connectTimeout);
    } else if (frame.type === "res") {
      this.handleResponse(frame, connectResolve, connectReject, connectTimeout);
    }
  }

  private handleEvent(
    frame: EventFrame,
    connectResolve?: (value: void) => void,
    connectReject?: (reason: Error) => void,
    connectTimeout?: ReturnType<typeof setTimeout>
  ) {
    if (frame.event === "connect.challenge") {
      // Received challenge, send connect request
      const connectReq = this.buildConnectRequest();
      this.ws?.send(JSON.stringify(connectReq));
      return;
    }

    if (frame.event === "tick") {
      this.resetTickTimer();
      return;
    }

    if (frame.event === "chat") {
      const event = frame.payload as ChatEvent;
      if (event?.runId) {
        const listeners = this.chatListeners.get(event.runId);
        if (listeners) {
          for (const listener of listeners) {
            listener(event);
          }
        }
      }
      return;
    }

    // Suppress unused variable warnings for connect handlers on other events
    void connectResolve;
    void connectReject;
    void connectTimeout;
  }

  private handleResponse(
    frame: ResponseFrame,
    connectResolve?: (value: void) => void,
    connectReject?: (reason: Error) => void,
    connectTimeout?: ReturnType<typeof setTimeout>
  ) {
    // Check if this is the hello-ok response to our connect request
    if (
      frame.ok &&
      frame.payload &&
      typeof frame.payload === "object" &&
      "type" in frame.payload &&
      (frame.payload as { type: string }).type === "hello-ok"
    ) {
      const hello = frame.payload as {
        policy?: { tickIntervalMs?: number };
      };
      this.tickIntervalMs = hello.policy?.tickIntervalMs ?? 30000;
      this.connected = true;
      this.connecting = false;
      this.reconnectDelay = RECONNECT_BASE_MS;
      this.resetTickTimer();
      if (connectTimeout) clearTimeout(connectTimeout);
      connectResolve?.();
      return;
    }

    // If connect failed
    if (!this.connected && !frame.ok) {
      if (connectTimeout) clearTimeout(connectTimeout);
      this.connecting = false;
      connectReject?.(
        new Error(
          frame.error?.message ?? "Connection rejected"
        )
      );
      return;
    }

    // Regular RPC response
    const pending = this.pending.get(frame.id);
    if (pending) {
      this.pending.delete(frame.id);
      clearTimeout(pending.timer);
      if (frame.ok) {
        pending.resolve(frame.payload);
      } else {
        pending.reject(
          new Error(
            frame.error?.message ?? "Request failed"
          )
        );
      }
    }
  }

  private buildConnectRequest(): RequestFrame {
    const params: ConnectParams = {
      minProtocol: 3,
      maxProtocol: 3,
      client: {
        id: "openclaw-control-ui",
        version: "0.2.0",
        platform: "linux",
        mode: "ui",
      },
      auth: { token: GATEWAY_TOKEN() },
      role: "operator",
      scopes: ["operator.admin"],
    };
    return {
      type: "req",
      id: randomUUID(),
      method: "connect",
      params,
    };
  }

  private resetTickTimer() {
    if (this.tickTimer) clearTimeout(this.tickTimer);
    this.tickTimer = setTimeout(() => {
      // Tick timeout — server not responding
      console.warn("[gateway] Tick timeout, reconnecting");
      this.ws?.close();
    }, this.tickIntervalMs * 2.5);
  }

  private onDisconnect() {
    this.connected = false;
    this.connecting = false;
    if (this.tickTimer) clearTimeout(this.tickTimer);

    // Reject all pending requests
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(new Error("Connection lost"));
      this.pending.delete(id);
    }

    // Clear all chat listeners
    this.chatListeners.clear();

    if (!this.destroyed) {
      this.scheduleReconnect();
    }
  }

  private scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect().catch(() => {
        // Exponential backoff
        this.reconnectDelay = Math.min(
          this.reconnectDelay * 2,
          RECONNECT_MAX_MS
        );
      });
    }, this.reconnectDelay);
  }

  /** Send an RPC request and wait for the response */
  async request<T = unknown>(
    method: string,
    params: unknown,
    timeoutMs = 30000
  ): Promise<T> {
    await this.ensureConnected();
    const id = randomUUID();
    const frame: RequestFrame = { type: "req", id, method, params };

    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Request timeout: ${method}`));
      }, timeoutMs);

      this.pending.set(id, {
        resolve: resolve as (p: unknown) => void,
        reject,
        timer,
      });
      this.ws?.send(JSON.stringify(frame));
    });
  }

  /** Subscribe to chat events for a specific runId */
  onChatEvent(runId: string, listener: ChatEventListener): () => void {
    let listeners = this.chatListeners.get(runId);
    if (!listeners) {
      listeners = new Set();
      this.chatListeners.set(runId, listeners);
    }
    listeners.add(listener);
    return () => {
      listeners!.delete(listener);
      if (listeners!.size === 0) {
        this.chatListeners.delete(runId);
      }
    };
  }

  // --- High-level chat methods ---

  async chatSend(
    sessionKey: string,
    message: string
  ): Promise<{ runId: string; events: AsyncIterable<ChatEvent> }> {
    const idempotencyKey = randomUUID();
    const params: ChatSendParams = {
      sessionKey,
      message,
      idempotencyKey,
    };

    const response = await this.request<ChatSendResponse>(
      "chat.send",
      params,
      60000
    );
    const runId = response.runId;

    // Create an async iterable that yields chat events
    const self = this;
    const events: AsyncIterable<ChatEvent> = {
      [Symbol.asyncIterator]() {
        const buffer: ChatEvent[] = [];
        let done = false;
        let waitResolve: (() => void) | null = null;

        const unsubscribe = self.onChatEvent(runId, (event) => {
          buffer.push(event);
          if (waitResolve) {
            waitResolve();
            waitResolve = null;
          }
          if (
            event.state === "final" ||
            event.state === "aborted" ||
            event.state === "error"
          ) {
            done = true;
          }
        });

        return {
          async next(): Promise<IteratorResult<ChatEvent>> {
            while (buffer.length === 0 && !done) {
              await new Promise<void>((resolve) => {
                waitResolve = resolve;
                // Safety timeout to prevent infinite hang (5 min for long responses)
                setTimeout(resolve, 300000);
              });
            }
            if (buffer.length > 0) {
              return { value: buffer.shift()!, done: false };
            }
            unsubscribe();
            return { value: undefined as unknown as ChatEvent, done: true };
          },
          async return() {
            unsubscribe();
            return { value: undefined as unknown as ChatEvent, done: true };
          },
        };
      },
    };

    return { runId, events };
  }

  async chatHistory(
    sessionKey: string,
    limit = 200
  ): Promise<ChatHistoryResponse> {
    const params: ChatHistoryParams = { sessionKey, limit };
    return this.request<ChatHistoryResponse>("chat.history", params);
  }

  async chatAbort(
    sessionKey: string,
    runId?: string
  ): Promise<ChatAbortResponse> {
    const params: ChatAbortParams = { sessionKey, runId };
    return this.request<ChatAbortResponse>("chat.abort", params);
  }

  async chatInject(
    sessionKey: string,
    message: string,
    label?: string
  ): Promise<ChatInjectResponse> {
    const params: ChatInjectParams = { sessionKey, message, label };
    return this.request<ChatInjectResponse>("chat.inject", params);
  }

  get isConnected(): boolean {
    return this.connected;
  }

  destroy() {
    this.destroyed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.tickTimer) clearTimeout(this.tickTimer);
    this.ws?.close();
  }
}

// Singleton instance
let instance: GatewayClient | null = null;

export function getGatewayClient(): GatewayClient {
  if (!instance) {
    instance = new GatewayClient();
  }
  return instance;
}

export { GatewayClient };
