/** Wire protocol types for the OpenClaw gateway */

// --- Frames ---

export interface RequestFrame {
  type: "req";
  id: string;
  method: string;
  params?: unknown;
}

export interface ResponseFrame {
  type: "res";
  id: string;
  ok: boolean;
  payload?: unknown;
  error?: { code: string; message: string; retryable?: boolean };
}

export interface EventFrame {
  type: "event";
  event: string;
  payload?: unknown;
  seq?: number;
}

export type Frame = RequestFrame | ResponseFrame | EventFrame;

// --- Connect handshake ---

export interface ConnectParams {
  minProtocol: number;
  maxProtocol: number;
  client: {
    id: string;
    version: string;
    platform: string;
    mode: string;
  };
  auth?: {
    token?: string;
  };
  role?: string;
  scopes?: string[];
}

export interface HelloOk {
  type: "hello-ok";
  protocol: number;
  server: { connId: string };
  features: {
    methods: string[];
    events: string[];
  };
  policy: {
    maxPayload: number;
    tickIntervalMs: number;
  };
}

// --- Chat methods ---

export interface ChatSendParams {
  sessionKey: string;
  message: string;
  idempotencyKey: string;
  timeoutMs?: number;
  attachments?: Array<{
    type?: string;
    mimeType?: string;
    fileName?: string;
    content?: unknown;
  }>;
}

export interface ChatSendResponse {
  runId: string;
  status: "started" | "ok" | "in_flight" | "error";
}

export interface ChatHistoryParams {
  sessionKey: string;
  limit?: number;
}

export interface ChatHistoryResponse {
  sessionKey: string;
  sessionId?: string;
  messages: ChatMessage[];
}

export interface ChatAbortParams {
  sessionKey: string;
  runId?: string;
}

export interface ChatAbortResponse {
  ok: boolean;
  aborted: boolean;
  runIds: string[];
}

export interface ChatInjectParams {
  sessionKey: string;
  message: string;
  label?: string;
}

export interface ChatInjectResponse {
  ok: boolean;
  messageId: string;
}

// --- Chat events ---

export interface ChatEvent {
  runId: string;
  sessionKey: string;
  seq: number;
  state: "delta" | "final" | "aborted" | "error";
  message?: ChatMessage;
  errorMessage?: string;
  usage?: Record<string, number>;
  stopReason?: string;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string | ContentBlock[];
}

export interface ContentBlock {
  type: "text" | "image" | "tool_use" | "tool_result";
  text?: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  [key: string]: any;
}

/** Extract plain text from a ChatMessage's content */
export function extractText(message: ChatMessage): string {
  if (typeof message.content === "string") return message.content;
  return message.content
    .filter((b) => b.type === "text" && b.text)
    .map((b) => b.text!)
    .join("");
}
