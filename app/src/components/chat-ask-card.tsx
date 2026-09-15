"use client";

/**
 * The card an agent's `approval_required` event renders as, and the only place
 * the Helm answers one.
 *
 * ONE event, TWO rows. `ask_user` writes a durable `agent_questions` row and
 * the engine's channel polls it; `permission_escalation._announce` emits the
 * same event name for a tool-permission escalation, whose request is an
 * in-RAM object in the ENGINE with a coroutine waiting on it. They are answered
 * at different endpoints, so the card branches on `kind`:
 *
 * `question`
 *     `POST /api/approvals/question/{id}` with `{answer}`. That row is what
 *     `agent_questions.answer_question` settles, what the CLI and Telegram
 *     settle too, and what the webchat channel polls to learn the answer. A
 *     second answer path would be a second opinion about what the person said.
 * `escalation`
 *     `POST /api/approvals/escalation/{id}` with `{approved}` and, for "allow
 *     for this session", `remember_session`. The bridge proxies it to
 *     `/api/admin/approvals/escalation/{id}`, where the waiting coroutine
 *     lives. Before this branch existed the card posted escalations to the
 *     QUESTION route: the operator saw "Answered", the id meant nothing there,
 *     and the agent sat blocked until its timeout denied the tool for it.
 *
 * The other difference is time. A question is durable — a later answer still
 * reaches it, which is what the expiry line says. An escalation is not: at
 * `timeout_seconds` the engine denies and the run moves on, so the countdown is
 * real and the buttons go away when it ends. Collecting a decision nobody is
 * waiting for would be the same lie in slower motion.
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

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { HelpCircle, ShieldAlert } from "lucide-react";

export type ApprovalKind = "question" | "escalation";

export interface ChatAskCardProps {
  /** The `agent_questions` row id, or the escalation's `request_id`. */
  id: string;
  /** Which row this is. Absent means the durable question, as it always did. */
  kind?: ApprovalKind;
  question: string;
  /** Fixed choices; empty means free text. Questions only. */
  options?: string[];
  expiresAt?: string | null;
  /** The tool the agent is asking to use. Escalations only. */
  tool?: string;
  /** How long the engine will wait before denying for itself. Escalations only. */
  timeoutSeconds?: number;
}

const OPERATOR_ONLY = "an operator has to answer this — it has been recorded for them";
const ALREADY_DECIDED = "The agent already decided this one for itself.";

/** The three answers, and the exact body each one sends. */
const ESCALATION_ANSWERS = [
  {
    testId: "escalation-allow-once",
    label: "Allow once",
    body: { approved: true },
    variant: "default" as const,
  },
  {
    testId: "escalation-allow-session",
    label: "Allow for this session",
    body: { approved: true, remember_session: true },
    variant: "outline" as const,
  },
  {
    testId: "escalation-deny",
    label: "Deny",
    body: { approved: false },
    variant: "ghost" as const,
  },
];

export function ChatAskCard({
  id,
  kind = "question",
  question,
  options = [],
  expiresAt,
  tool,
  timeoutSeconds,
}: ChatAskCardProps) {
  const isEscalation = kind === "escalation";
  const [text, setText] = useState("");
  const [status, setStatus] = useState<string>("");
  const [settled, setSettled] = useState(false);
  const [sending, setSending] = useState(false);

  const budget =
    isEscalation && typeof timeoutSeconds === "number" && timeoutSeconds > 0
      ? Math.floor(timeoutSeconds)
      : null;
  /** Pinned, so a re-render cannot restart the agent's clock — but re-pinned
   * when the card is handed a different row (see below). */
  const [deadline, setDeadline] = useState<number | null>(
    budget === null ? null : Date.now() + budget * 1000
  );
  const [secondsLeft, setSecondsLeft] = useState<number | null>(budget);

  /** A different `id` is a different decision, so none of the state above may
   * survive the swap.
   *
   * The panel renders ONE card slot, so React reuses this instance whenever the
   * run raises a second approval. Without this reset, `settled`, `status` and
   * the pinned `deadline` carried over: the second escalation appeared with its
   * buttons already disabled and the previous answer still printed under it —
   * "the operator saw Answered and the agent sat blocked until its timeout",
   * which is the precise failure this card was written to remove, reintroduced
   * for every approval after the first.
   *
   * The panel also passes `key={id}`, which would be enough for the panel. This
   * is here as well because a component that is only correct for one caller's
   * render discipline is a trap for the next caller. Adjusting state during
   * render is React's documented way to derive from a changed prop; it costs
   * one extra render pass and no effect round-trip. */
  const [renderedId, setRenderedId] = useState(id);
  if (renderedId !== id) {
    setRenderedId(id);
    setSettled(false);
    setSending(false);
    setStatus("");
    setText("");
    setDeadline(budget === null ? null : Date.now() + budget * 1000);
    setSecondsLeft(budget);
  }

  /** The engine's own clock, mirrored. When it runs out, so do the buttons.
   *
   * Counted against a fixed deadline rather than by decrementing, because a
   * backgrounded tab throttles timers and a card that lost ten ticks would
   * still be offering buttons long after the engine denied the tool.
   *
   * Mirrored, not trusted: the countdown starts when the card renders, a
   * network hop after the engine started waiting, so it reads slightly LONG and
   * never short. Saying "expired" a moment late is corrected by the 404 the
   * route answers with; expiring early would take a live decision off screen. */
  useEffect(() => {
    if (deadline === null || settled) return;
    const timer = setInterval(() => {
      const left = Math.ceil((deadline - Date.now()) / 1000);
      if (left > 0) {
        setSecondsLeft(left);
        return;
      }
      setSecondsLeft(null);
      setSettled(true);
      setStatus(ALREADY_DECIDED);
    }, 1000);
    return () => clearInterval(timer);
  }, [deadline, settled]);

  const post = useCallback(
    async (path: string, body: Record<string, unknown>, settledMessage: string) => {
      if (settled || sending) return;
      setSending(true);
      try {
        const res = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
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
        if (payload.settled === false) {
          // Somebody (or, for an escalation, the timeout) got there first. The
          // row is closed either way, so the card is too — it just does not
          // claim this answer was the one recorded.
          setSettled(true);
          setSecondsLeft(null);
          setStatus(isEscalation ? ALREADY_DECIDED : payload.message || "Already answered — nothing changed.");
          return;
        }
        if (!res.ok) {
          setStatus(`That did not reach the agent (${res.status}). Try again.`);
          return;
        }
        setSettled(true);
        setSecondsLeft(null);
        setStatus(settledMessage);
      } catch (err) {
        setStatus(`That did not reach the agent: ${(err as Error).message}`);
      } finally {
        setSending(false);
      }
    },
    [isEscalation, settled, sending]
  );

  const submit = useCallback(
    async (answer: string) => {
      const clean = answer.trim();
      if (!clean) return;
      await post(
        `/api/bridge/api/approvals/question/${id}`,
        { answer: clean },
        `Answered: ${clean}`
      );
    },
    [id, post]
  );

  const decide = useCallback(
    async (body: Record<string, unknown>, label: string) => {
      await post(`/api/bridge/api/approvals/escalation/${id}`, body, `Answered: ${label}`);
    },
    [id, post]
  );

  if (isEscalation) {
    return (
      <div className="flex justify-start" data-testid="escalation-card">
        <div className="max-w-[90%] space-y-3 rounded-lg border border-warning/30 bg-warning/5 p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm font-semibold text-warning">
            <ShieldAlert className="h-4 w-4" />
            <span>Permission needed</span>
            <span
              className="rounded border border-warning/40 px-1.5 py-0 font-mono text-[11px] font-normal"
              data-testid="escalation-tool"
            >
              {tool || "a tool"}
            </span>
          </div>
          <p className="whitespace-pre-wrap text-sm" data-testid="ask-question">
            {question}
          </p>

          <div className="flex flex-wrap gap-2">
            {ESCALATION_ANSWERS.map((answer) => (
              <Button
                key={answer.testId}
                size="sm"
                variant={answer.variant}
                disabled={settled || sending}
                onClick={() => decide(answer.body, answer.label)}
                data-testid={answer.testId}
              >
                {answer.label}
              </Button>
            ))}
          </div>

          {secondsLeft !== null && !settled && (
            <p className="text-xs text-muted-foreground" data-testid="escalation-countdown">
              The agent denies this itself in {secondsLeft}s.
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
