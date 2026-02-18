import { apiFetch } from "./client";
import type { MemorySearchResult, MemoryEntity } from "./types";

interface QueryResponse {
  answer: string;
  query: string;
  memories_found: number;
  web_results_found: number;
  sources: {
    memory: Array<{
      tier: string;
      type: string;
      similarity: number;
      preview: string;
    }>;
    web: Array<{
      title: string;
      url: string;
    }>;
  };
}

export async function searchMemory(
  query: string,
  limit = 10
): Promise<MemorySearchResult[]> {
  try {
    const res = await apiFetch<QueryResponse>("/api/orchestrator/query", {
      method: "POST",
      body: JSON.stringify({ question: query, limit }),
    });
    // Convert the orchestrator response into MemorySearchResult format
    const results: MemorySearchResult[] =
      res.sources?.memory?.map((m) => ({
        content: m.preview,
        similarity: m.similarity,
        category: m.type,
        created_at: "",
      })) ?? [];
    // Add the AI answer as the first result
    if (res.answer) {
      results.unshift({
        content: res.answer,
        similarity: 1.0,
        category: "answer",
        created_at: "",
      });
    }
    return results;
  } catch {
    return [];
  }
}

export async function storeMemory(
  content: string,
  contentType = "conversation"
): Promise<{ status: string }> {
  return apiFetch<{ status: string }>("/api/orchestrator/ingest", {
    method: "POST",
    body: JSON.stringify({
      content,
      channel: "app",
      content_type: contentType,
    }),
  });
}

export async function getEntity(name: string): Promise<MemoryEntity | null> {
  try {
    return await apiFetch<MemoryEntity>(
      `/api/orchestrator/entity/${encodeURIComponent(name)}`
    );
  } catch {
    return null;
  }
}
