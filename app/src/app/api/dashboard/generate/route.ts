import { getDashboardSystemPrompt, buildEnrichedPrompt } from "@/lib/dashboard/system-prompt";
import { validateDashboardCode, detectCodeType } from "@/lib/dashboard/code-validator";
import { fetchDataForNeeds } from "@/lib/dashboard/conversation-context";
import { triageDashboard } from "@/lib/dashboard/triage-prompt";
import { isTrivialResponse } from "@/lib/dashboard/topic-detector";
import type { ConversationMessage } from "@/lib/dashboard/topic-detector";

const OPENROUTER_API_KEY = () => process.env.OPENROUTER_API_KEY || "";
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "google/gemini-2.5-flash";

export async function POST(req: Request) {
  const body = await req.json();

  // New: triage-driven conversation path (v2)
  if (body.messages && Array.isArray(body.messages)) {
    return handleTriagedDashboard(body.messages as ConversationMessage[]);
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
async function handleTriagedDashboard(messages: ConversationMessage[]) {
  // Fast client-side guard: skip if both sides are trivial
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const userTrivial = !lastUser || isTrivialResponse(lastUser.content);
  const assistantTrivial = !lastAssistant || isTrivialResponse(lastAssistant.content);

  if (userTrivial && assistantTrivial) {
    return new Response(null, { status: 204 });
  }

  // Phase 1: Triage — should dashboard update?
  const apiKey = OPENROUTER_API_KEY();
  const triage = await triageDashboard(messages, apiKey);

  if (!triage.shouldUpdate) {
    return new Response(null, { status: 204 });
  }

  // Phase 2: Fetch data based on triage dataNeeds
  const enrichedData = await fetchDataForNeeds(triage.dataNeeds);

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
      const err = await response.text().catch(() => "Unknown error");
      return new Response(
        JSON.stringify({ error: `OpenRouter error: ${err}` }),
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
      return new Response(
        JSON.stringify({
          error: `Generated code failed validation: ${validation.errors.join(", ")}`,
        }),
        { status: 422, headers: { "Content-Type": "application/json" } }
      );
    }

    return new Response(
      JSON.stringify({ html: validation.code, type: codeType }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }
    );
  } catch (err) {
    return new Response(
      JSON.stringify({ error: `Dashboard generation error: ${String(err)}` }),
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
