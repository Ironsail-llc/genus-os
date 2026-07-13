import { fetchWelcomeContext } from "@/lib/dashboard/welcome-context";
import { getDashboardSystemPrompt, getTimeAwarePrompt } from "@/lib/dashboard/system-prompt";
import { validateDashboardCode, detectCodeType } from "@/lib/dashboard/code-validator";
import { getEngineClient } from "@/lib/engine/server-client";
import DOMPurify from "isomorphic-dompurify";
import { SANITIZE_CONFIG } from "../generate/route";

/**
 * Build a data-bound welcome prompt that gives the model explicit values to use.
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
  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      const keepalive = setInterval(() => {
        controller.enqueue(encoder.encode(" "));
      }, 10_000);

      try {
        const context = await fetchWelcomeContext();
        const systemPrompt = getDashboardSystemPrompt();
        const userPrompt = buildWelcomeUserPrompt(context);
        const fullCode = await getEngineClient().dashboardCompletion(
          "render",
          systemPrompt,
          userPrompt,
        );
        const validation = validateDashboardCode(fullCode);
        const codeType = detectCodeType(validation.code);

        if (!validation.valid) {
          console.error("[dashboard-error] source=welcome-validation |", validation.errors.join("; "), "| code_length:", fullCode.length, "| first_100:", fullCode.slice(0, 100));
          controller.enqueue(encoder.encode(
            JSON.stringify({ error: "Generated dashboard failed quality check", errors: validation.errors })
          ));
        } else {
          const sanitized = DOMPurify.sanitize(validation.code, SANITIZE_CONFIG);
          controller.enqueue(encoder.encode(
            JSON.stringify({ html: sanitized, type: codeType, sanitized: true })
          ));
        }
      } catch {
        console.error("[welcome] Generation failed");
        controller.enqueue(encoder.encode(
          JSON.stringify({ error: "Dashboard service temporarily unavailable" })
        ));
      } finally {
        clearInterval(keepalive);
        controller.close();
      }
    },
  });

  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
