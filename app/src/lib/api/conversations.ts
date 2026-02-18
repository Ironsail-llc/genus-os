import { apiFetch } from "./client";
import type { Conversation, Message } from "./types";

interface ConversationsResponse {
  data: {
    meta: Record<string, number>;
    payload: Conversation[];
  };
}

export async function fetchConversations(
  status?: string
): Promise<Conversation[]> {
  const params = status ? `?status=${status}` : "";
  const res = await apiFetch<ConversationsResponse>(
    `/api/bridge/api/conversations${params}`
  );
  return res.data?.payload ?? [];
}

export async function fetchConversation(id: number): Promise<Conversation> {
  return apiFetch<Conversation>(`/api/bridge/api/conversations/${id}`);
}

export async function fetchMessages(
  conversationId: number
): Promise<Message[]> {
  return apiFetch<Message[]>(
    `/api/bridge/api/conversations/${conversationId}/messages`
  );
}

export async function sendMessage(
  conversationId: number,
  content: string,
  isPrivate = false
): Promise<Message> {
  return apiFetch<Message>(
    `/api/bridge/api/conversations/${conversationId}/messages`,
    {
      method: "POST",
      body: JSON.stringify({ content, private: isPrivate }),
    }
  );
}
