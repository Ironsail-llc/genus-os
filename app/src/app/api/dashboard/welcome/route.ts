import { fetchWelcomeContext } from "@/lib/dashboard/welcome-context";
import { getDashboardSystemPrompt, getTimeAwarePrompt } from "@/lib/dashboard/system-prompt";
import { validateDashboardCode, detectCodeType } from "@/lib/dashboard/code-validator";
import DOMPurify from "isomorphic-dompurify";
import { SANITIZE_CONFIG } from "../generate/route";

const OPENROUTER_API_KEY = () => process.env.OPENROUTER_API_KEY || "";
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "google/gemini-2.5-flash-lite-preview-09-2025";

/**
 * Build a data-bound welcome prompt that gives Gemini explicit values to use.
 * This prevents hallucination — the model only sees real numbers.
 */
function buildWelcomeUserPrompt(context: Awaited<ReturnType<typeof fetchWelcomeContext>>): string {
  const timePrompt = getTimeAwarePrompt(context.hour);
  const parts: string[] = [timePrompt];

  // Present each data source explicitly with its actual values
  parts.push("\n## Real Data (use ONLY these values — never invent numbers)");

  parts.push(`\nGreeting: "${context.greeting}"`);
  parts.push(`Date: ${context.dayOfWeek}, ${new Date(context.timestamp).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}`);

  if (context.health) {
    const healthy = context.health.services.filter(s => s.status === "healthy").length;
    const total = context.health.services.length;
    const pct = total > 0 ? Math.round((healthy / total) * 100) : 0;
    parts.push(`\nService Health: ${healthy}/${total} healthy (${pct}%) — status: "${context.health.status}"`);
    for (const s of context.health.services) {
      parts.push(`  - ${s.name}: ${s.status}`);
    }
  } else {
    parts.push("\nService Health: unavailable (skip this section)");
  }

  if (context.inbox) {
    parts.push(`\nInbox: ${context.inbox.openCount} open conversations, ${context.inbox.unreadCount} unread`);
  } else {
    parts.push("\nInbox: unavailable (skip this section)");
  }

  if (context.calendar) {
    parts.push(`\nCalendar: ${context.calendar}`);
  } else {
    parts.push("\nCalendar: no events found (skip this section)");
  }

  if (context.eventBus) {
    parts.push(`\nEvent Bus: ${context.eventBus.total} total events across ${Object.keys(context.eventBus.streams).length} streams`);
  } else {
    parts.push("\nEvent Bus: unavailable (skip this section)");
  }

  parts.push(`\n## DATA INTEGRITY RULES
- Display ONLY the exact numbers shown above. Never round up, estimate, or invent.
- If a section says "unavailable" or "skip", do NOT render a card for it.
- If a section says "0" for a count, show 0 — do not replace with a made-up number.
- The dashboard must accurately reflect the current system state. No placeholders.

Generate the dashboard HTML now. No markdown fences, no explanation, no code fences.`);

  return parts.join("\n");
}

export async function POST() {
  try {
    const context = await fetchWelcomeContext();
    const systemPrompt = getDashboardSystemPrompt();
    const userPrompt = buildWelcomeUserPrompt(context);

    const response = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${OPENROUTER_API_KEY()}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://app.robothor.ai",
        "X-Title": "Robothor Welcome Dashboard",
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
      console.error("[welcome] OpenRouter error:", await response.text().catch(() => "Unknown"));
      return new Response(
        JSON.stringify({ error: "Dashboard service temporarily unavailable" }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      );
    }

    // Accumulate the full response (buffered, not streamed)
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

    const validation = validateDashboardCode(fullCode);
    const codeType = detectCodeType(validation.code);

    if (!validation.valid) {
      console.error("[dashboard-error] source=welcome-validation |", validation.errors.join("; "), "| code_length:", fullCode.length, "| first_100:", fullCode.slice(0, 100));
      return new Response(
        JSON.stringify({ error: "Generated dashboard failed quality check", errors: validation.errors }),
        { status: 422, headers: { "Content-Type": "application/json" } }
      );
    }

    const sanitized = DOMPurify.sanitize(validation.code, SANITIZE_CONFIG);

    return new Response(
      JSON.stringify({ html: sanitized, type: codeType, sanitized: true }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }
    );
  } catch (err) {
    console.error("[welcome] Generation error:", err);
    return new Response(
      JSON.stringify({ error: "Dashboard generation failed" }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
}
