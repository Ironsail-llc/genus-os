/**
 * Dashboard triage — quick Gemini Flash call (~256 tokens, ~1s) that decides
 * whether the dashboard needs updating and what data to fetch.
 *
 * Replaces the regex-based topic detection in topic-detector.ts.
 */

const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "google/gemini-2.5-flash";

export interface TriageResult {
  shouldUpdate: boolean;
  dataNeeds: string[];
  summary: string;
}

const TRIAGE_SYSTEM_PROMPT = `You are a dashboard triage agent. Given a conversation between a user and an AI assistant, decide:
1. Should the visual dashboard update? (YES for substantive topics, NO for trivial acks like "ok", "thanks", greetings)
2. If YES, what data sources should be fetched?

Available data sources:
- "health" — service health checks (bridge, orchestrator, vision)
- "contacts" — CRM contacts/people list
- "conversations" — open conversations/inbox
- "companies" — CRM companies list
- "calendar" — today's meetings/events
- "memory:<query>" — search memory/knowledge base for <query>
- "web:<query>" — web search for <query> (weather, news, research, etc.)
- "overview" — combined health + inbox summary

Respond with ONLY valid JSON, no markdown fences:
{"shouldUpdate": true/false, "dataNeeds": ["source1", "source2"], "summary": "brief description of what dashboard should show"}

Examples:
- User says "thanks" → {"shouldUpdate": false, "dataNeeds": [], "summary": ""}
- User asks about weather → {"shouldUpdate": true, "dataNeeds": ["web:weather NYC today"], "summary": "Current weather conditions for NYC"}
- User asks about services → {"shouldUpdate": true, "dataNeeds": ["health"], "summary": "Service health status dashboard"}
- User asks about contacts at Acme → {"shouldUpdate": true, "dataNeeds": ["contacts", "companies"], "summary": "Contacts and company info for Acme"}
- User asks about schedule → {"shouldUpdate": true, "dataNeeds": ["calendar"], "summary": "Today's schedule and upcoming meetings"}
- User discusses project details → {"shouldUpdate": true, "dataNeeds": ["memory:project details"], "summary": "Project information dashboard"}
- User says "ok" or "got it" → {"shouldUpdate": false, "dataNeeds": [], "summary": ""}
- Assistant gives weather info → {"shouldUpdate": true, "dataNeeds": ["web:weather"], "summary": "Weather conditions dashboard"}`;

/**
 * Build the user prompt for triage from recent conversation messages.
 */
export function buildTriageUserPrompt(
  messages: Array<{ role: string; content: string }>
): string {
  const recent = messages.slice(-4);
  const lines = recent.map(
    (m) => `${m.role === "user" ? "User" : "Assistant"}: ${m.content.slice(0, 500)}`
  );
  return `Conversation:\n${lines.join("\n")}\n\nShould the dashboard update? What data is needed?`;
}

/**
 * Call Gemini Flash for a quick triage decision.
 * Returns structured TriageResult.
 */
export async function triageDashboard(
  messages: Array<{ role: string; content: string }>,
  apiKey: string
): Promise<TriageResult> {
  const userPrompt = buildTriageUserPrompt(messages);

  try {
    const response = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://app.robothor.ai",
        "X-Title": "Robothor Dashboard Triage",
      },
      body: JSON.stringify({
        model: MODEL,
        messages: [
          { role: "system", content: TRIAGE_SYSTEM_PROMPT },
          { role: "user", content: userPrompt },
        ],
        max_tokens: 256,
        temperature: 0.1,
        response_format: { type: "json_object" },
      }),
      signal: AbortSignal.timeout(5000),
    });

    if (!response.ok) {
      return { shouldUpdate: false, dataNeeds: [], summary: "" };
    }

    const data = await response.json();
    const content = data.choices?.[0]?.message?.content || "";

    // Parse JSON from response (strip markdown fences if present)
    const cleaned = content.replace(/```json\n?/g, "").replace(/```\n?/g, "").trim();
    const parsed = JSON.parse(cleaned);

    return {
      shouldUpdate: Boolean(parsed.shouldUpdate),
      dataNeeds: Array.isArray(parsed.dataNeeds) ? parsed.dataNeeds : [],
      summary: String(parsed.summary || ""),
    };
  } catch {
    // On any error, don't update (safe default)
    return { shouldUpdate: false, dataNeeds: [], summary: "" };
  }
}
