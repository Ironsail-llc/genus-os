"use client";

/**
 * The card an agent's question renders as, and the only place the Helm answers
 * one.
 *
 * It posts to the bridge's existing `POST /api/approvals/question/{id}` through
 * the BFF proxy, which attaches the session's bearer. No new Next route and no
 * new engine endpoint: that row is what `agent_questions.answer_question`
 * settles, what the CLI and Telegram settle too, and what the webchat channel
 * polls to learn the answer. A second answer path would be a second opinion
 * about what the person said.
 *
 * Two failures it must never hide:
 *
 * - A refusal. `require_operator` admits owner/admin only, so a member's own
 *   question 403s today (the answerer-matches-addressee allowance belongs with
 *   the ask-binding rules, not here). The card says an operator has to answer
 *   rather than looking like a network hiccup.
 * - An already-settled row. `{settled: false}` means somebody else answered
 *   first; showing the returned message is the honest outcome, and re-enabling
 *   the buttons would invite a person to answer a question that is closed.
 *
 * A transport failure leaves the card ENABLED: the answer has not been recorded,
 * so the one thing that must not happen is the person believing it has.
 */

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { HelpCircle } from "lucide-react";

export interface ChatAskCardProps {
  /** The `agent_questions` row id. */
  id: string;
  question: string;
  /** Fixed choices; empty means free text. */
  options?: string[];
  expiresAt?: string | null;
}

const OPERATOR_ONLY = "an operator has to answer this — it has been recorded for them";

export function ChatAskCard({ id, question, options = [], expiresAt }: ChatAskCardProps) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<string>("");
  const [settled, setSettled] = useState(false);
  const [sending, setSending] = useState(false);

  const submit = useCallback(
    async (answer: string) => {
      const clean = answer.trim();
      if (!clean || settled || sending) return;
      setSending(true);
      try {
        const res = await fetch(`/api/bridge/api/approvals/question/${id}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answer: clean }),
        });
        let payload: { settled?: boolean; message?: string } = {};
        try {
          payload = await res.json();
        } catch {
          // A body that is not JSON tells us nothing beyond the status.
        }
        if (res.status === 403) {
          setStatus(OPERATOR_ONLY);
          return;
        }
        if (!res.ok) {
          setStatus(`That did not reach the agent (${res.status}). Try again.`);
          return;
        }
        if (payload.settled === false) {
          // Somebody answered first. The row is closed either way, so the card
          // is too — it just does not claim this answer was the one recorded.
          setSettled(true);
          setStatus(payload.message || "Already answered — nothing changed.");
          return;
        }
        setSettled(true);
        setStatus(`Answered: ${clean}`);
      } catch (err) {
        setStatus(`That did not reach the agent: ${(err as Error).message}`);
      } finally {
        setSending(false);
      }
    },
    [id, settled, sending],
  );

  return (
    <div className="flex justify-start" data-testid="ask-card">
      <div className="max-w-[90%] space-y-3 rounded-lg border border-primary/30 bg-primary/5 p-4">
        <div className="flex items-center gap-2 text-sm font-semibold text-primary">
          <HelpCircle className="h-4 w-4" />
          <span>Question</span>
        </div>
        <p className="text-sm" data-testid="ask-question">
          {question}
        </p>

        {options.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {options.map((option, index) => (
              <Button
                key={`${option}-${index}`}
                size="sm"
                disabled={settled || sending}
                onClick={() => submit(option)}
                data-testid={`ask-option-${index}`}
              >
                {option}
              </Button>
            ))}
          </div>
        ) : (
          <div className="space-y-2">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              disabled={settled}
              placeholder="Your answer…"
              className="min-h-[60px] w-full resize-none rounded-md border border-primary/30 bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary/50"
              data-testid="ask-input"
            />
            <Button
              size="sm"
              disabled={settled || sending}
              onClick={() => submit(text)}
              data-testid="ask-submit"
            >
              Answer
            </Button>
          </div>
        )}

        {expiresAt && !settled && (
          <p className="text-xs text-muted-foreground" data-testid="ask-expires">
            The agent stops waiting at {new Date(expiresAt).toLocaleTimeString()}, but a later
            answer still reaches it.
          </p>
        )}
        {status && (
          <p className="text-xs text-muted-foreground" data-testid="ask-status">
            {status}
          </p>
        )}
      </div>
    </div>
  );
}
