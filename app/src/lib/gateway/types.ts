/** Chat types for the Agent Engine HTTP API */

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}
