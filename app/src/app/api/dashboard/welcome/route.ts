import { fetchWelcomeContext } from "@/lib/dashboard/welcome-context";
import { getDashboardSystemPrompt, getTimeAwarePrompt } from "@/lib/dashboard/system-prompt";
import { validateDashboardCode, detectCodeType } from "@/lib/dashboard/code-validator";

const OPENROUTER_API_KEY = () => process.env.OPENROUTER_API_KEY || "";
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const MODEL = "google/gemini-2.5-flash";

export async function POST() {
  try {
    const context = await fetchWelcomeContext();
    const systemPrompt = getDashboardSystemPrompt();
    const timePrompt = getTimeAwarePrompt(context.hour);

    const userPrompt = `${timePrompt}

Context data:
${JSON.stringify(context, null, 2)}

Generate the dashboard HTML now. No markdown fences, no explanation, no code fences.`;

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
        max_tokens: 2048,
        temperature: 0.4,
      }),
    });

    if (!response.ok || !response.body) {
      const err = await response.text().catch(() => "Unknown error");
      return new Response(
        JSON.stringify({ error: `OpenRouter error: ${err}` }),
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
      JSON.stringify({ error: `Welcome dashboard error: ${String(err)}` }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
}
