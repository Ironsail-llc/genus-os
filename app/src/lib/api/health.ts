import { apiFetch } from "./client";
import type { HealthResponse } from "./types";

export async function fetchHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/api/health");
}
