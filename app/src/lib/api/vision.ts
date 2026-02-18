import { apiFetch } from "./client";
import type { VisionStatus } from "./types";

export async function fetchVisionStatus(): Promise<VisionStatus> {
  return apiFetch<VisionStatus>("/api/vision/health");
}

export async function visionLook(
  prompt?: string
): Promise<{ description: string }> {
  return apiFetch<{ description: string }>("/api/vision/look", {
    method: "POST",
    body: JSON.stringify({ prompt: prompt || "Describe what you see" }),
  });
}

export async function setVisionMode(
  mode: "disarmed" | "basic" | "armed"
): Promise<{ mode: string }> {
  return apiFetch<{ mode: string }>("/api/vision/mode", {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
}
