import { getDashboardSystemPrompt, buildEnrichedPrompt } from "@/lib/dashboard/system-prompt";
import { validateDashboardCode, detectCodeType } from "@/lib/dashboard/code-validator";
import { fetchDataForNeeds } from "@/lib/dashboard/conversation-context";
import { triageDashboard } from "@/lib/dashboard/triage-prompt";
import { isTrivialResponse } from "@/lib/dashboard/topic-detector";
import type { ConversationMessage } from "@/lib/dashboard/topic-detector";
import DOMPurify from "isomorphic-dompurify";

export const SANITIZE_CONFIG = {
  ADD_TAGS: ["canvas", "svg", "polyline", "path", "circle", "rect", "line", "text", "g", "defs", "linearGradient", "stop", "form", "textarea", "select", "input"],
  ADD_ATTR: ["data-chart", "data-testid", "data-tab", "data-sort-dir", "viewBox", "points", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "fill", "d", "cx", "cy", "r", "x1", "y1", "x2", "y2", "offset", "stop-color", "stop-opacity", "height", "width", "onclick", "onsubmit", "placeholder", "rows", "required", "disabled"],
  ALLOW_DATA_ATTR: true,
  ALLOW_UNKNOWN_PROTOCOLS: false,
  FORBID_TAGS: ["iframe", "object", "embed", "meta"],
  FORBID_ATTR: ["onerror", "onload", "onmouseover", "onfocus", "onblur"],
};

const OPENROUTER_API_KEY = () => process.env.OPENROUTER_API_KEY || "";
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "google/gemini-2.5-flash-lite-preview-09-2025";

const RATE_LIMIT_WINDOW = 60_000;
const RATE_LIMIT_MAX = 10;
const requestLog: number[] = [];

function isRateLimited(): boolean {
  const now = Date.now();
  const cutoff = now - RATE_LIMIT_WINDOW;
  // Find first entry within the window — O(1) amortized via splice
  const firstValid = requestLog.findIndex((t) => t >= cutoff);
  if (firstValid > 0) requestLog.splice(0, firstValid);
  else if (firstValid === -1) requestLog.length = 0;
  if (requestLog.length >= RATE_LIMIT_MAX) return true;
  requestLog.push(now);
  return false;
}

export async function POST(req: Request) {
  if (isRateLimited()) {
    return new Response(JSON.stringify({ error: "Too many requests" }), {
      status: 429,
      headers: { "Content-Type": "application/json", "Retry-After": "60" },
    });
  }
  const body = await req.json();

  // New: triage-driven conversation path (v2)
  if (body.messages && Array.isArray(body.messages)) {
    const agentData = body.agentData && typeof body.agentData === "object"
      ? (body.agentData as Record<string, unknown>)
      : undefined;
    return handleTriagedDashboard(body.messages as ConversationMessage[], agentData);
  }

  // Legacy: intent-based path (still supported for backward compat)
  const { intent, context, data } = body as {
    intent: string;
    context?: Record<string, unknown>;
    data?: unknown;
  };

  if (!intent) {
    return new Response(JSON.stringify({ error: "intent required" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const systemPrompt = getDashboardSystemPrompt();
  const userPrompt = buildUserPrompt(intent, context, data);

  return generateBuffered(systemPrompt, userPrompt);
}

/**
 * New two-phase pipeline:
 * 1. Quick trivial check (client-side guard) + LLM triage (~1s)
 * 2. If shouldUpdate: fetch data → generate dashboard → return buffered JSON
 */
/**
 * Check if a data need is already satisfied by agent-provided data.
 * Matches the need prefix against agentData keys.
 * e.g., agentData.web satisfies "web:weather NYC", agentData.health satisfies "health"
 */
function isNeedSatisfied(need: string, agentData?: Record<string, unknown>): boolean {
  if (!agentData) return false;
  const prefix = need.split(":")[0];
  return prefix in agentData;
}

async function handleTriagedDashboard(messages: ConversationMessage[], agentData?: Record<string, unknown>) {
  // Fast client-side guard: skip if both sides are trivial
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const userTrivial = !lastUser || isTrivialResponse(lastUser.content);
  const assistantTrivial = !lastAssistant || isTrivialResponse(lastAssistant.content);

  if (userTrivial && assistantTrivial) {
    console.log("[dashboard] Skipped: both sides trivial");
    return new Response(null, { status: 204 });
  }

  // Phase 1: Triage + speculative data fetch in parallel.
  // Start fetching commonly-needed data ("overview" = health + inbox) while
  // triage runs, saving ~1s when triage decides it needs health/inbox/overview.
  const apiKey = OPENROUTER_API_KEY();
  const [triage, speculativeData] = await Promise.all([
    triageDashboard(messages, apiKey),
    fetchDataForNeeds(["health", "conversations"]).catch(() => ({})),
  ]);
  console.log("[dashboard] Triage:", JSON.stringify({ shouldUpdate: triage.shouldUpdate, dataNeeds: triage.dataNeeds, summary: triage.summary }));

  if (!triage.shouldUpdate) {
    return new Response(null, { status: 204 });
  }

  // Phase 2: Fetch only what the agent didn't provide and speculation didn't cover
  const unsatisfiedNeeds = triage.dataNeeds.filter(
    (need) => !isNeedSatisfied(need, agentData) && !isNeedSatisfied(need, speculativeData)
  );
  console.log("[dashboard] Fetching:", unsatisfiedNeeds, "| Speculative:", Object.keys(speculativeData), "| Agent provided:", agentData ? Object.keys(agentData) : "none");
  const fetchedData = await fetchDataForNeeds(unsatisfiedNeeds);
  const enrichedData = { ...speculativeData, ...fetchedData, ...agentData };
  console.log("[dashboard] Data keys:", Object.keys(enrichedData));

  // Phase 3: Generate dashboard HTML (buffered, not streamed)
  const systemPrompt = getDashboardSystemPrompt();
  const userPrompt = buildEnrichedPrompt(messages, enrichedData, triage.summary);

  return generateBuffered(systemPrompt, userPrompt);
}

/**
 * Generate dashboard HTML via OpenRouter and return as buffered JSON.
 * Returns: { html: string, type: "html" } on 200, or error on 502/500.
 */
async function generateBuffered(systemPrompt: string, userPrompt: string) {
  try {
    const response = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${OPENROUTER_API_KEY()}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://app.robothor.ai",
        "X-Title": "Robothor Dashboard",
      },
      body: JSON.stringify({
        model: MODEL,
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: userPrompt },
        ],
        stream: true,
        max_tokens: 4096,
        temperature: 0.3,
      }),
    });

    if (!response.ok || !response.body) {
      console.error("[dashboard] OpenRouter error:", await response.text().catch(() => "Unknown"));
      return new Response(
        JSON.stringify({ error: "Dashboard service temporarily unavailable" }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      );
    }

    // Accumulate the full response (buffered, not streamed to client)
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullCode = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6).trim();
        if (data === "[DONE]") continue;

        try {
          const parsed = JSON.parse(data);
          const chunk = parsed.choices?.[0]?.delta?.content || "";
          if (chunk) {
            fullCode += chunk;
          }
        } catch {
          // Skip malformed chunks
        }
      }
    }

    // Validate the generated code
    const validation = validateDashboardCode(fullCode);
    const codeType = detectCodeType(validation.code);

    if (!validation.valid) {
      console.error("[dashboard-error] source=server-validation |", validation.errors.join("; "), "| code_length:", fullCode.length, "| first_100:", fullCode.slice(0, 100));
      return new Response(
        JSON.stringify({ error: "Generated dashboard failed quality check", errors: validation.errors }),
        { status: 422, headers: { "Content-Type": "application/json" } }
      );
    }

    // Sanitize server-side so the client can skip DOMPurify (saves 50-200ms + 40KB bundle)
    const sanitized = DOMPurify.sanitize(validation.code, SANITIZE_CONFIG);

    return new Response(
      JSON.stringify({ html: sanitized, type: codeType, sanitized: true }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }
    );
  } catch (err) {
    console.error("[dashboard] Generation error:", err);
    return new Response(
      JSON.stringify({ error: "Dashboard generation failed" }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
}

function buildUserPrompt(
  intent: string,
  context?: Record<string, unknown>,
  data?: unknown
): string {
  const parts: string[] = [`Generate a dashboard for intent: "${intent}"`];

  if (context && Object.keys(context).length > 0) {
    parts.push(`\nContext:\n${JSON.stringify(context, null, 2)}`);
  }

  if (data) {
    parts.push(
      `\nData to display:\n${JSON.stringify(data, null, 2).slice(0, 3000)}`
    );
  }

  parts.push("\nOutput HTML only. No markdown fences, no explanation, no code fences.");

  return parts.join("\n");
}
