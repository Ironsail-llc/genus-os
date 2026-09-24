"use client";

import { useState, useRef, useEffect, useCallback, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useRuntimeConfig } from "@/components/runtime-config";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
} from "@/components/ui/tooltip";
import { useVisualState } from "@/hooks/use-visual-state";
import { useThrottle } from "@/hooks/use-throttle";
import { MarkerInterceptor, stripMarkers } from "@/lib/engine/marker-interceptor";
import { ChatRecovery, type ChatRecoveryRequest } from "@/components/chat-recovery";
import { ChatAskCard, type ApprovalKind } from "@/components/chat-ask-card";
import {
  isKeyableAgentId,
  readStoredChatAgent,
  storeChatAgent,
} from "@/lib/chat/agent-session";
import { Send, Square, Check, X, ClipboardList, MessageSquareText, Brain } from "lucide-react";

import { forgetRequest, journalRequest, pendingRequests } from "@/lib/chat/request-journal";

import { OUTCOME_UNKNOWN, requestFailure, terminalOutcome } from "@/lib/chat/terminal-outcome";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
  recovery?: ChatRecoveryRequest;
  /** Where a message sent while the agent was working went (live follow-ups). */
  note?: string;
}

/** A message typed while a turn ran, waiting to be sent as the next turn. */
interface QueuedFollowup {
  id: string;
  text: string;
}

const JOINED_NOTE = "Added to the running task";
const QUEUED_NOTE = "Queued — sends when this reply finishes";

interface ActivePlan {
  plan_id: string;
  plan_text: string;
  original_message: string;
  status: string;
  deep_plan?: boolean;
}

/** A decision the agent is waiting on, from the run's `approval_required` event.
 *
 * TWO rows wear this one event name. `ask_user` writes a durable
 * `agent_questions` row; `permission_escalation` announces an in-RAM
 * tool-permission request the engine is blocked on. `kind` is what the card
 * needs to pick the route that settles it, and `tool`/`timeout_seconds` are the
 * two fields only the escalation carries. */
interface ActiveAsk {
  id: string;
  kind: ApprovalKind;
  question: string;
  options: string[];
  expires_at?: string | null;
  tool?: string;
  timeout_seconds?: number;
}

/** One agent the chat may be pointed at. */
interface ChattableAgent {
  id: string;
  name: string;
}

/** Strip any residual markers from messages (history or live).
 *  Delegates to the balanced walk in marker-interceptor: a regex that stops
 *  at the first `}]` leaves everything after a nested array on screen. */
function stripResidualMarkers(text: string): string {
  return stripMarkers(text).trim();
}

interface ChatPanelProps {
  /** When true, renders in mobile-optimized layout (no duplicate header, labeled controls) */
  mobile?: boolean;
}

export function ChatPanel({ mobile = false }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const recoveryScopes = useRef<Record<string, string>>({});
  const requestScopes = useRef<Record<string, string>>({});
  const recoverMessage = useCallback((id: string, text: string, plan?: ActivePlan) => {
    if (plan) setActivePlan(current => current ?? plan);
    setMessages((prev) => prev.map((item) => {
      if (item.id !== id) return item;
      if (item.recovery?.scope) forgetRequest(item.recovery.scope, item.recovery.requestId);
      return { ...item, content: stripResidualMarkers(text), recovery: undefined };
    }));
  }, []);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [activePlan, setActivePlan] = useState<ActivePlan | null>(null);
  // A LIST, not a slot. A run can raise a second approval before the first
  // is answered, and each escalation is a coroutine blocked in the engine:
  // one that never reaches the screen is not a card the operator can scroll
  // back to, it is a tool that runs out its whole timeout and denies.
  const [activeAsks, setActiveAsks] = useState<ActiveAsk[]>([]);
  // Which agent the chat is pointed at, `""` meaning the appliance's default.
  //
  // `""` is load-bearing: it is what makes every request below carry NO `agent`
  // field, which is what makes the engine resolve the main session — the one
  // the operator's webchat and Telegram share on purpose, holding every message
  // they have ever sent. `chattable` starts empty, so until the fleet listing
  // answers there is no switcher and the panel behaves exactly as it did.
  //
  // It starts at `""` and is only ever set to an id the fleet listing has
  // CONFIRMED chattable. An earlier version seeded it synchronously from
  // `localStorage` to save a round-trip, which meant the mount-time history,
  // plan and deep requests all carried a key before anything could veto it —
  // and for a member, whose listing is refused outright, no veto was coming.
  // The saving was one read of the operator's own main history; the cost was a
  // key on three requests for a session the caller may not be entitled to.
  const [agent, setAgent] = useState("");
  const [chattable, setChattable] = useState<ChattableAgent[]>([]);
  const [defaultAgent, setDefaultAgent] = useState("");
  const [isPlanExecuting, setIsPlanExecuting] = useState(false);
  const [planMode, setPlanMode] = useState(false);
  const [isPlanning, setIsPlanning] = useState(false);
  const [showFeedbackInput, setShowFeedbackInput] = useState(false);
  const [planFeedback, setPlanFeedback] = useState("");
  const [activeToolName, setActiveToolName] = useState<string | null>(null);
  const [runtimeProgress, setRuntimeProgress] = useState<string | null>(null);
  const [stopNotice, setStopNotice] = useState<string | null>(null);
  const [isStopping, setIsStopping] = useState(false);
  const requestIdRef = useRef<string | null>(null);
  const [currentIteration, setCurrentIteration] = useState(0);
  const [maxIterations, setMaxIterations] = useState(0);
  const [deepMode, setDeepMode] = useState(false);
  const [deepPlan, setDeepPlan] = useState(false);
  const [isDeepReasoning, setIsDeepReasoning] = useState(false);
  const [deepElapsed, setDeepElapsed] = useState(0);
  const [, setDeepCost] = useState<{ time_s: number; cost: number } | null>(null);
  const throttledStreamingText = useThrottle(streamingText, 100);
  const scrollEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Live follow-ups: messages typed while a turn runs. The engine takes them
  // into that turn (`followup_joined`) or hands them back (`followup_queued`,
  // `pending_followups`); those are sent as the next ordinary turn when the
  // running stream ends, so a message is delayed at worst, never dropped.
  const followupQueue = useRef<QueuedFollowup[]>([]);
  const joinedFollowups = useRef<QueuedFollowup[]>([]);
  const [queuedCount, setQueuedCount] = useState(0);
  const { notifyConversationUpdate, setRender } = useVisualState();
  const { aiName } = useRuntimeConfig();

  /** `?agent=…`, or nothing at all for the main agent. */
  const agentQuery = agent ? `?agent=${encodeURIComponent(agent)}` : "";

  /** Whether the listing left out the agent it named as the default.
   *
   * It happens: a YAML typo puts a manifest in the listing's `broken` bucket
   * rather than in `agents`. The default option below is synthesised in that
   * case, because the alternative — dropping it — leaves the `<select>` value
   * matching no option, so the browser shows row 0 (some worker) while the
   * panel is still on the main session, and nothing on screen gets the
   * operator back. */
  const defaultAgentMissing = Boolean(
    defaultAgent && !chattable.some((row) => row.id === defaultAgent)
  );

  /** Every option the switcher offers. Index 0 is always the main agent.
   *
   * Its value is the EMPTY STRING, never an id. "Send no session key" is the
   * property that keeps the operator's shared main session shared, and an id
   * here is a value that can drift from that meaning — a listing that names no
   * `default_agent` would leave the panel comparing against `""` and sending
   * `agent: "main"` for the one choice that must send nothing. The label is
   * where the agent's name goes. */
  const agentOptions = useMemo(() => {
    const others = chattable.filter((row) => row.id !== defaultAgent);
    const named = chattable.find((row) => row.id === defaultAgent);
    return [{ id: "", name: named?.name || defaultAgent || aiName }, ...others];
  }, [chattable, defaultAgent, aiName]);

  const currentAgentName = chattable.find((row) => row.id === agent)?.name ?? aiName;

  // Scroll to bottom on new messages (throttled during streaming)
  useEffect(() => {
    scrollEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, throttledStreamingText, activePlan, isDeepReasoning]);

  /** An `approval_required` event: the agent asked something and is waiting.
   *
   * Keyed on the row id and idempotent, because the same event is emitted twice
   * for a webchat run — once by the `ask_user` tool and once by the channel that
   * waits on the row — and two cards for one question would be a lie about how
   * many answers are needed.
   *
   * No id, no card: the answer is a POST to that row, so a card without one
   * could not carry an answer anywhere. */
  const handleApprovalRequired = useCallback((parsed: Record<string, unknown>) => {
    const id = typeof parsed.id === "string" ? parsed.id : "";
    if (!id) return;
    setActiveAsks((current) => {
      if (current.some((ask) => ask.id === id)) return current;
      return [
        ...current,
        {
          id,
          kind: parsed.kind === "escalation" ? "escalation" : "question",
          question: typeof parsed.question === "string" ? parsed.question : "",
          options: Array.isArray(parsed.options) ? (parsed.options as string[]) : [],
          expires_at: typeof parsed.expires_at === "string" ? parsed.expires_at : null,
          tool: typeof parsed.tool === "string" ? parsed.tool : undefined,
          timeout_seconds:
            typeof parsed.timeout_seconds === "number" ? parsed.timeout_seconds : undefined,
        },
      ];
    });
  }, []);

  /** The events EVERY stream handles identically. Returns true when consumed.
   *
   * The panel reads three SSE streams — `/chat/send`, `/chat/plan/start` (also
   * the revise path) and `/chat/plan/approve` (also deep-plan execution) — and
   * each one grew its own event `if` ladder. `approval_required` was added to
   * two of them, which left the stream that matters most uncovered:
   * `plan/approve` is the FULL-TOOLS execution run (`trigger_detail=
   * "plan-exec:…"`), i.e. the one Helm run where `ask_user` actually fires. With
   * the webchat channel now waiting on the durable row, a dropped event there is
   * not a missing card — it is a run blocked for the whole tool budget on a
   * question nobody was shown, and then reported to the model as "asked and
   * stayed silent".
   *
   * So the cross-stream events live here, once, and every reader calls this
   * first. A new stream that forgets to is a missing call to one function rather
   * than a missing branch in a ladder nobody reads. */
  const handleSharedStreamEvent = useCallback(
    (eventType: string, parsed: Record<string, unknown>): boolean => {
      if (eventType === "approval_required") {
        handleApprovalRequired(parsed);
        return true;
      }
      if (["accepted", "queued", "waiting", "progress", "stopping"].includes(eventType)) {
        const activity = typeof parsed.text === "string" ? parsed.text : eventType;
        const elapsed = typeof parsed.elapsed_s === "number" && !activity.includes("elapsed")
          ? ` · ${parsed.elapsed_s}s elapsed` : "";
        setRuntimeProgress(activity + elapsed);
        return true;
      }
      return false;
    },
    [handleApprovalRequired],
  );

  /** Which agents this appliance will let the chat address, and which one is
   * the default.
   *
   * `chattable` comes from the bridge, not from a rule spelled here: an agent
   * is chattable when it HOLDS a session between runs
   * (`schedule.session_target: persistent`) or when it is the configured
   * default. The listing is operator-gated, so a member simply gets no switcher
   * — and no switcher is exactly today's behaviour, on the main session, which
   * is the right way for this to degrade. */
  useEffect(() => {
    let cancelled = false;
    fetch("/api/bridge/api/agent-manifests")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled) return;
        const rows: ChattableAgent[] = Array.isArray(data?.agents)
          ? (data.agents as Array<Record<string, unknown>>)
              .filter(
                (row) =>
                  row.chattable === true &&
                  typeof row.id === "string" &&
                  // The listing is the allowlist for WHICH agents; this is the
                  // only thing the browser judges for itself, and it judges one
                  // thing: whether `agent:<id>:primary` still parses as that
                  // shape. An id carrying the key's own delimiter does not, and
                  // an option the appliance cannot honour must not be offered.
                  isKeyableAgentId(row.id)
              )
              .map((row) => ({ id: String(row.id), name: String(row.name || row.id) }))
          : [];
        const fallback = typeof data?.default_agent === "string" ? data.default_agent : "";
        setChattable(rows);
        setDefaultAgent(fallback);
        // The remembered agent is adopted HERE and nowhere earlier. Seeding it
        // before the first render put a key on the mount-time history, plan and
        // deep requests before this listing could veto it — and for a member,
        // whose listing is refused outright, there is no veto coming at all.
        // Under `ROBOTHOR_PER_USER_SESSIONS=observe|off` the engine honours a
        // member's requested key verbatim, so "the default mode contains it" is
        // not a property this panel gets to rely on.
        const remembered = readStoredChatAgent();
        setAgent(
          remembered && remembered !== fallback && rows.some((row) => row.id === remembered)
            ? remembered
            : ""
        );
      })
      .catch(() => {
        // No listing means no switcher — and therefore no way back. A
        // remembered agent adopted here would send every message to a
        // conversation the person cannot see they are in, with no control on
        // screen to leave it. Unreachable stays on the main agent.
        if (!cancelled) setAgent("");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** Point the chat at another agent. Its history replaces what is on screen.
   *
   * The previous agent's messages go with it. Leaving them would put two
   * conversations in one scrollback with nothing to tell them apart, and the
   * next thing the person typed would be read by an agent that never said any
   * of it. */
  const switchAgent = useCallback(
    (next: string) => {
      // `next` is an option value, so `""` already means the main agent and
      // there is no id to compare against. Anything else must be an agent the
      // listing confirmed chattable; a `<select>` cannot produce another value,
      // and if some future caller does, the main session is where it lands.
      const chosen = chattable.some((row) => row.id === next) && next !== defaultAgent ? next : "";
      if (chosen === agent) return;
      setActiveAsks([]);
      setActivePlan(null);
      storeChatAgent(chosen);
      setAgent(chosen);
    },
    [agent, chattable, defaultAgent]
  );

  // Load history on mount, and again whenever the chat is pointed elsewhere
  //
  // Cancelled on the way out, like the listing above it. Both the abandoned
  // agent's request and the new one `setMessages([])` and then resolve whenever
  // they resolve; without this token the ABANDONED transcript can land last and
  // win, so the operator reads agent A's conversation under agent B's name and
  // the next thing they type goes to B.
  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    fetch(`/api/chat/history${agentQuery}`)
      .then((res) => {
        if (res.ok === false) throw new Error("History unavailable");
        return res.json();
      })
      .then((data) => {
        if (cancelled) return;
        const scope = typeof data.recoveryScope === "string" ? data.recoveryScope : undefined;
        recoveryScopes.current[agent] = scope ?? "";
        {
          const loaded: ChatMessage[] = (Array.isArray(data.messages) ? data.messages : [])
            .filter(
              (m: { role: string }) =>
                m.role === "user" || m.role === "assistant"
            )
            .map(
              (
                m: { role: "user" | "assistant"; content: string | Array<{ type: string; text?: string }> },
                i: number
              ) => ({
                id: `hist-${i}`,
                role: m.role,
                content: stripResidualMarkers(
                  typeof m.content === "string"
                    ? m.content
                    : m.content
                        .filter((b) => b.type === "text")
                        .map((b) => b.text || "")
                        .join("")
                ),
                timestamp: new Date(),
              })
            );
          const restored: ChatMessage[] = scope ? pendingRequests(scope)
            .filter((requestId) => requestId !== requestIdRef.current)
            .map((requestId) => ({
              id: `recovery-${requestId}`, role: "assistant", content: OUTCOME_UNKNOWN,
              timestamp: new Date(), recovery: { requestId, agent, scope },
            })) : [];
          setMessages([...loaded, ...restored]);
        }
      })
      .catch(() => {
        // Engine not available on mount
      });
    return () => {
      cancelled = true;
    };
  }, [agentQuery, agent]);

  // Recover pending plan on page refresh
  //
  // Cancelled for a sharper reason than the transcript: a plan recovered from
  // the ABANDONED agent's session would become `activePlan` while `agent` is
  // somebody else, and Approve would then post that plan_id with the new
  // agent's key — executing a plan against a session that never saw it, which
  // is the thing threading the key through plan/approve exists to prevent.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/chat/plan/status${agentQuery}`)
      .then((res) => res.json())
      .then((data) => {
        if (cancelled) return;
        if (data.active && data.plan) {
          setActivePlan({
            plan_id: data.plan.plan_id,
            plan_text: data.plan.plan_text,
            original_message: data.plan.original_message,
            status: data.plan.status,
            deep_plan: data.plan.deep_plan || false,
          });
        }
      })
      .catch(() => {
        // Engine not available
      });
    return () => {
      cancelled = true;
    };
  }, [agentQuery, agent]);

  // Keyboard shortcut: Ctrl/Cmd+Shift+P toggles plan mode, Ctrl/Cmd+Shift+D toggles deep+plan
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "P") {
        e.preventDefault();
        setPlanMode((prev) => !prev);
        setDeepMode(false);
        setDeepPlan(false);
        inputRef.current?.focus();
      }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "D") {
        e.preventDefault();
        setDeepMode((prev) => {
          const next = !prev;
          if (next) {
            setPlanMode(true);
            setDeepPlan(true);
          } else {
            setPlanMode(false);
            setDeepPlan(false);
          }
          return next;
        });
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Recover active deep reasoning on page refresh
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/chat/deep/status${agentQuery}`)
      .then((res) => res.json())
      .then((data) => {
        if (cancelled) return;
        if (data.active && data.deep?.status === "running") {
          setIsDeepReasoning(true);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [agentQuery, agent]);

  const sendPlanMessage = useCallback(async (overrideText?: string) => {
    const text = overrideText || input.trim();
    if (!text || isStreaming || isPlanning) return;

    // A new message means the person moved on, so a finished question stops
    // sitting above a fresh conversation. It cannot strand an OPEN one: the
    // composer is disabled for the whole time a run is in flight
    // (`isStreaming || isPlanExecuting || isPlanning || isDeepReasoning` on the
    // textarea and the send button), which is exactly the window in which a run
    // is waiting on an answer.
    //
    // No claim that the Helm can bring a dropped card back: it has no approvals
    // view yet. The row stays answerable through the bridge's approvals endpoint
    // and a later turn sees the answer.
    setActiveAsks([]);

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: text,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);
    if (!overrideText) setInput("");
    setIsPlanning(true);
    setStreamingText("");

    const controller = new AbortController();
    abortRef.current = controller;
    requestIdRef.current = crypto.randomUUID();
    setStopNotice(null);
    setIsStopping(false);
    setRuntimeProgress("Submitting request…");

    let submitted = false;
    try {
      const requestId = requestIdRef.current!;
      const scope = await journalRequest(agent, requestId, recoveryScopes.current[agent], controller.signal);
      if (scope) requestScopes.current[requestId] = scope;
      controller.signal.throwIfAborted();
      submitted = true;
      const res = await fetch("/api/chat/plan/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, request_id: requestIdRef.current, deep_plan: deepPlan, ...(agent ? { agent } : {}) }),
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        const failure = await requestFailure(res);
        if (failure.rejected && scope) forgetRequest(scope, requestId);
        setMessages((prev) => [
          ...prev,
          { id: `err-${Date.now()}`, role: "assistant", content: failure.text, timestamp: new Date(),
            ...(!failure.rejected ? { recovery: { requestId, agent, scope } } : {}),
          },
        ]);
        setIsPlanning(false);
        setStreamingText("");
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let sseEventType = "";
      let sseData = "";
      let fullResponse = "";
      let gotPlanEvent = false;
      let receivedTerminal = false;

      const handleSSEEvent = (eventType: string, data: string) => {
        try {
          const parsed = JSON.parse(data);
          if (handleSharedStreamEvent(eventType, parsed)) {
            return;
          }
          if (eventType === "delta") {
            fullResponse += parsed.text || "";
            setStreamingText(fullResponse);
          } else if (eventType === "plan") {
            gotPlanEvent = true;
            setActivePlan({
              plan_id: parsed.plan_id,
              plan_text: parsed.plan_text,
              original_message: parsed.original_message,
              status: parsed.status,
              deep_plan: parsed.deep_plan || false,
            });
          } else if (eventType === "tool_start") {
            setActiveToolName(parsed.tool || null);
          } else if (eventType === "tool_end") {
            setActiveToolName(null);
          } else if (eventType === "done") {
            const terminal = terminalOutcome(parsed, fullResponse);
            if (terminal !== undefined) { receivedTerminal = true; fullResponse = terminal; }
          }
        } catch { /* skip */ }
      };

      const processLine = (line: string) => {
        if (line === "") {
          if (sseData) handleSSEEvent(sseEventType || "delta", sseData);
          sseEventType = "";
          sseData = "";
        } else if (line.startsWith("event: ")) {
          sseEventType = line.slice(7);
        } else if (line.startsWith("data: ")) {
          sseData += (sseData ? "\n" : "") + line.slice(6);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sseBuffer += decoder.decode(value, { stream: true });
        const lines = sseBuffer.split("\n");
        sseBuffer = lines.pop() || "";
        for (const line of lines) processLine(line);
      }
      sseBuffer += decoder.decode();
      if (sseBuffer) {
        for (const line of sseBuffer.split("\n")) processLine(line);
      }
      if (sseData) handleSSEEvent(sseEventType || "delta", sseData);

      setIsPlanning(false);
      setStreamingText("");

      if (!gotPlanEvent && !receivedTerminal) fullResponse = OUTCOME_UNKNOWN;
      if (gotPlanEvent || fullResponse !== OUTCOME_UNKNOWN) {
        if (scope) forgetRequest(scope, requestId);
      }
      // If no plan event was received, show the response as a regular message
      if (!gotPlanEvent && fullResponse.trim()) {
        setMessages((prev) => [
          ...prev,
          {
            id: `asst-${Date.now()}`,
            role: "assistant",
            content: stripResidualMarkers(fullResponse).trim(),
        ...(fullResponse === OUTCOME_UNKNOWN ? { recovery: { requestId: requestIdRef.current!, agent, scope: requestScopes.current[requestIdRef.current!] } } : {}),
            timestamp: new Date(),
          },
        ]);
      }
    } catch {
      const requestId = requestIdRef.current!;
      const scope = requestScopes.current[requestId];
      if (!submitted && scope) forgetRequest(scope, requestId);
      setMessages((prev) => [...prev, {
        id: `err-${Date.now()}`, role: "assistant", timestamp: new Date(),
        content: submitted ? OUTCOME_UNKNOWN : "Plan preparation stopped before submission.",
        ...(submitted ? { recovery: { requestId, agent, scope } } : {}),
      }]);
    } finally {
      setIsPlanning(false);
      setStreamingText("");
      setActiveToolName(null);
      abortRef.current = null;
    }
  }, [input, isStreaming, isPlanning, deepPlan, handleSharedStreamEvent, agent]);

  const noteMessages = useCallback((ids: string[], note: string | undefined) => {
    setMessages((prev) => prev.map((m) => (ids.includes(m.id) ? { ...m, note } : m)));
  }, []);

  const queueFollowups = useCallback((items: QueuedFollowup[]) => {
    if (!items.length) return;
    followupQueue.current = [...followupQueue.current, ...items];
    setQueuedCount(followupQueue.current.length);
    noteMessages(items.map((i) => i.id), QUEUED_NOTE);
  }, [noteMessages]);

  /** Typed while a turn runs: offer it to that turn; anything else waits for the next. */
  const sendFollowup = useCallback(async (text: string) => {
    const id = `user-${Date.now()}`;
    setMessages((prev) => [...prev, { id, role: "user", content: text, timestamp: new Date(), note: "Sending…" }]);
    setInput("");
    let joined = false;
    try {
      const res = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, join_running: true, ...(agent ? { agent } : {}) }),
      });
      joined = res.ok && (await res.text()).includes("event: followup_joined");
    } catch {
      joined = false;
    }
    if (joined) {
      joinedFollowups.current = [...joinedFollowups.current, { id, text }];
      noteMessages([id], JOINED_NOTE);
    } else {
      queueFollowups([{ id, text }]);
    }
  }, [agent, noteMessages, queueFollowups]);

  const sendMessage = useCallback(async (queued?: QueuedFollowup[] | unknown) => {
    const flushing = Array.isArray(queued) ? (queued as QueuedFollowup[]) : null;
    if (!flushing && (deepMode || planMode)) {
      // Deep mode routes through plan mode (deep always plans first).
      // Standalone plan mode also uses sendPlanMessage.
      return sendPlanMessage();
    }

    const text = flushing ? flushing.map((q) => q.text).join("\n\n") : input.trim();
    if (!flushing && text && isStreaming) return sendFollowup(text);
    if (!text || isStreaming) return;

    setActiveAsks([]);

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: text,
      timestamp: new Date(),
    };
    if (flushing) {
      // Their bubbles are already on screen, where they were typed.
      noteMessages(flushing.map((q) => q.id), undefined);
    } else {
      setMessages((prev) => [...prev, userMsg]);
      setInput("");
    }
    setIsStreaming(true);
    setStreamingText("");

    const controller = new AbortController();
    abortRef.current = controller;
    requestIdRef.current = crypto.randomUUID();
    setStopNotice(null);
    setIsStopping(false);
    setRuntimeProgress("Submitting request…");

    try {
      const requestId = requestIdRef.current!;
      const scope = await journalRequest(agent, requestId, recoveryScopes.current[agent], controller.signal);
      if (scope) requestScopes.current[requestId] = scope;
      if (controller.signal.aborted) {
        if (scope) forgetRequest(scope, requestId);
        controller.signal.throwIfAborted();
      }
      const res = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, request_id: requestIdRef.current, ...(agent ? { agent } : {}) }),
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        const failure = await requestFailure(res);
        const scope = requestScopes.current[requestIdRef.current!];
        if (failure.rejected && scope) forgetRequest(scope, requestIdRef.current!);
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`, role: "assistant",
          content: failure.text, timestamp: new Date(),
          ...(!failure.rejected ? { recovery: { requestId: requestIdRef.current!, agent, scope } } : {}),
        };
        setMessages((prev) => [...prev, errorMsg]);
        setIsStreaming(false);
        setStreamingText("");
        setActiveToolName(null);
        setCurrentIteration(0);
        setMaxIterations(0);
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let sseEventType = "";
      let sseData = "";
      let fullResponse = "";
      let receivedTerminal = false;
      const collectedAgentData: Record<string, unknown> = {};

      const interceptor = new MarkerInterceptor();

      const handleSSEEvent = (eventType: string, data: string) => {
        try {
          const parsed = JSON.parse(data);
          if (handleSharedStreamEvent(eventType, parsed)) {
            return;
          }
          if (eventType === "delta") {
            // Run through marker interceptor to strip [DASHBOARD:...] / [RENDER:...] markers
            const result = interceptor.addChunk(parsed.text || "");
            if (result.text) {
              fullResponse += result.text;
              setStreamingText(fullResponse);
            }
            for (const marker of result.markers) {
              if (marker.type === "dashboard" && marker.data && typeof marker.data === "object") {
                Object.assign(collectedAgentData, marker.data as Record<string, unknown>);
              } else if (marker.type === "render") {
                setRender({
                  component: (marker as { component: string }).component,
                  props: (marker as { props: Record<string, unknown> }).props,
                });
              }
            }
          } else if (eventType === "dashboard") {
            if (parsed.data && typeof parsed.data === "object") {
              Object.assign(collectedAgentData, parsed.data as Record<string, unknown>);
            }
          } else if (eventType === "render") {
            setRender({
              component: parsed.component,
              props: parsed.props,
            });
          } else if (eventType === "plan") {
            // Plan event from engine — show approval card
            setActivePlan({
              plan_id: parsed.plan_id,
              plan_text: parsed.plan_text,
              original_message: parsed.original_message,
              status: parsed.status,
              deep_plan: parsed.deep_plan || false,
            });
          } else if (eventType === "tool_start") {
            setActiveToolName(parsed.tool || null);
          } else if (eventType === "tool_end") {
            setActiveToolName(null);
          } else if (eventType === "iteration_start") {
            setCurrentIteration(parsed.iteration || 0);
            setMaxIterations(parsed.max_iterations || 0);
          } else if (eventType === "interim") {
            // An answer the agent superseded because a follow-up arrived while
            // it was being written: it stands as its own message, and the
            // revised one streams in after it.
            interceptor.flush();
            const content = stripResidualMarkers(String(parsed.text ?? fullResponse)).trim();
            if (content) {
              setMessages((prev) => [...prev, { id: `asst-interim-${Date.now()}`, role: "assistant", content, timestamp: new Date() }]);
            }
            fullResponse = "";
            setStreamingText("");
          } else if (eventType === "pending_followups") {
            // Never taken by the turn: they go out as the next turn instead.
            const pending: string[] = Array.isArray(parsed.messages) ? parsed.messages : [];
            const items = pending.map((text, n) => {
              const at = joinedFollowups.current.findIndex((j) => j.text === text);
              const match = at >= 0 ? joinedFollowups.current.splice(at, 1)[0] : null;
              return { id: match?.id ?? `user-${Date.now()}-${n}`, text };
            });
            queueFollowups(items);
          } else if (eventType === "done") {
            // Flush any buffered text from marker interceptor
            const flushed = interceptor.flush();
            if (flushed.text) {
              fullResponse += flushed.text;
            }
            for (const marker of flushed.markers) {
              if (marker.type === "dashboard" && marker.data && typeof marker.data === "object") {
                Object.assign(collectedAgentData, marker.data as Record<string, unknown>);
              } else if (marker.type === "render") {
                setRender({
                  component: (marker as { component: string }).component,
                  props: (marker as { props: Record<string, unknown> }).props,
                });
              }
            }
            const terminal = terminalOutcome(parsed, fullResponse);
            if (terminal !== undefined) {
              receivedTerminal = true;
              fullResponse = terminal;
            }
          }
        } catch {
          // Invalid JSON, skip
        }
      };

      // Proper SSE parser: buffer event type + data until empty-line boundary.
      // The old parser used lines[i-1] to detect event types, which broke when
      // TCP chunks split between the "event:" and "data:" lines — the done event
      // would be misidentified as a delta, doubling the message text.
      const processLine = (line: string) => {
        if (line === "") {
          // Empty line = SSE event boundary — dispatch accumulated event
          if (sseData) {
            handleSSEEvent(sseEventType || "delta", sseData);
          }
          sseEventType = "";
          sseData = "";
        } else if (line.startsWith("event: ")) {
          sseEventType = line.slice(7);
        } else if (line.startsWith("data: ")) {
          sseData += (sseData ? "\n" : "") + line.slice(6);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        sseBuffer += decoder.decode(value, { stream: true });
        const lines = sseBuffer.split("\n");
        sseBuffer = lines.pop() || "";

        for (const line of lines) {
          processLine(line);
        }
      }

      // Flush TextDecoder and process any remaining buffer data
      sseBuffer += decoder.decode();
      if (sseBuffer.length > 0) {
        const remaining = sseBuffer.split("\n");
        for (const line of remaining) {
          processLine(line);
        }
      }
      // Dispatch any accumulated event that wasn't terminated by an empty line
      if (sseData) {
        handleSSEEvent(sseEventType || "delta", sseData);
      }

      // Clear streaming state BEFORE adding the message to avoid duplicate display
      setIsStreaming(false);
      setStreamingText("");
      setActiveToolName(null);
      setCurrentIteration(0);
      setMaxIterations(0);

      if (!receivedTerminal) fullResponse = OUTCOME_UNKNOWN;
      else if (fullResponse !== OUTCOME_UNKNOWN) {
        const scope = requestScopes.current[requestIdRef.current!];
        if (scope) forgetRequest(scope, requestIdRef.current!);
      }

      // Finalize the message
      const assistantMsg: ChatMessage = {
        id: `asst-${Date.now()}`,
        role: "assistant",
        content: stripResidualMarkers(fullResponse).trim(),
        ...(fullResponse === OUTCOME_UNKNOWN ? { recovery: { requestId: requestIdRef.current!, agent, scope: requestScopes.current[requestIdRef.current!] } } : {}),
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, assistantMsg]);

      // Only trigger dashboard update when there's dashboard-relevant content.
      // Short conversational replies (< 200 chars, no agent data) skip the
      // triage+generate pipeline entirely, saving 5-11s of LLM + fetch time.
      const hasAgentData = Object.keys(collectedAgentData).length > 0;
      const isSubstantive = assistantMsg.content.length >= 200;
      if (receivedTerminal && (hasAgentData || isSubstantive)) {
        const recentMessages = [
          { role: userMsg.role, content: userMsg.content },
          { role: assistantMsg.role, content: assistantMsg.content },
        ].filter((m) => m.content.trim());

        notifyConversationUpdate(
          recentMessages,
          hasAgentData ? collectedAgentData : undefined
        );
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`,
          role: "assistant",
          content: OUTCOME_UNKNOWN,
          recovery: { requestId: requestIdRef.current!, agent, scope: requestScopes.current[requestIdRef.current!] },
          timestamp: new Date(),
        };
        setMessages((prev) => [...prev, errorMsg]);
      }
    } finally {
      setIsStreaming(false);
      setStreamingText("");
      abortRef.current = null;
      joinedFollowups.current = [];
    }
  }, [input, isStreaming, planMode, deepMode, sendPlanMessage, sendFollowup, noteMessages, queueFollowups, notifyConversationUpdate, setRender, handleSharedStreamEvent, agent]);

  // Once nothing is running, what waited goes out as one ordinary turn.
  const sendMessageRef = useRef(sendMessage);
  sendMessageRef.current = sendMessage;
  useEffect(() => {
    if (!queuedCount || isStreaming || isPlanning || isPlanExecuting || isDeepReasoning) return;
    const items = followupQueue.current;
    followupQueue.current = [];
    setQueuedCount(0);
    void sendMessageRef.current(items);
  }, [queuedCount, isStreaming, isPlanning, isPlanExecuting, isDeepReasoning]);

  const handlePlanApprove = useCallback(async () => {
    if (!activePlan) return;
    const isDeepPlan = activePlan.deep_plan === true;
    setPlanMode(false);
    setDeepMode(false);
    setDeepPlan(false);
    setIsPlanExecuting(true);
    setStreamingText("");

    const controller = new AbortController();
    abortRef.current = controller;
    requestIdRef.current = crypto.randomUUID();
    setStopNotice(null);
    setIsStopping(false);
    setRuntimeProgress("Submitting request…");

    // Show deep reasoning progress if this is a deep plan
    if (isDeepPlan) {
      setIsDeepReasoning(true);
      setDeepElapsed(0);
      setDeepCost(null);
    }

    try {
      const requestId = requestIdRef.current!;
      const scope = await journalRequest(agent, requestId, recoveryScopes.current[agent], controller.signal);
      if (scope) requestScopes.current[requestId] = scope;
      if (controller.signal.aborted) {
        if (scope) forgetRequest(scope, requestId);
        controller.signal.throwIfAborted();
      }
      const res = await fetch("/api/chat/plan/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: activePlan.plan_id, request_id: requestIdRef.current, ...(agent ? { agent } : {}) }),
        signal: controller.signal,
      });

      setActivePlan(null);

      if (!res.ok || !res.body) {
        const failure = await requestFailure(res);
        const scope = requestScopes.current[requestIdRef.current!];
        if (failure.rejected && scope) forgetRequest(scope, requestIdRef.current!);
        setMessages((prev) => [...prev, {
          id: `err-${Date.now()}`, role: "assistant", content: failure.text, timestamp: new Date(),
          ...(!failure.rejected ? { recovery: { requestId: requestIdRef.current!, agent, scope } } : {}),
        }]);
        return;
      }

      // Stream the execution response
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let sseEventType = "";
      let sseData = "";
      let fullResponse = "";
      let receivedTerminal = false;
      let costInfo: { time_s: number; cost: number } | null = null;

      const processLine = (line: string) => {
        if (line === "") {
          if (sseData) {
            try {
              const parsed = JSON.parse(sseData);
              if (handleSharedStreamEvent(sseEventType, parsed)) {
                sseEventType = "";
                sseData = "";
                return;
              }
              if (sseEventType === "delta") {
                fullResponse += parsed.text || "";
                setStreamingText(fullResponse);
              } else if (sseEventType === "deep_progress") {
                setDeepElapsed(parsed.elapsed_s || 0);
              } else if (sseEventType === "deep_result") {
                fullResponse = parsed.response || fullResponse;
                costInfo = {
                  time_s: parsed.execution_time_s || 0,
                  cost: parsed.cost_usd || 0,
                };
              } else if (sseEventType === "done") {
                const terminal = terminalOutcome(parsed, fullResponse);
                if (terminal !== undefined) {
                  receivedTerminal = true;
                  fullResponse = terminal;
                }
                if (parsed.cost_usd && !costInfo) {
                  costInfo = {
                    time_s: parsed.execution_time_s || 0,
                    cost: parsed.cost_usd || 0,
                  };
                }
              } else if (sseEventType === "error") {
                fullResponse = `Error: ${parsed.error || "Unknown error"}`;
              }
            } catch { /* skip */ }
          }
          sseEventType = "";
          sseData = "";
        } else if (line.startsWith("event: ")) {
          sseEventType = line.slice(7);
        } else if (line.startsWith("data: ")) {
          sseData += (sseData ? "\n" : "") + line.slice(6);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sseBuffer += decoder.decode(value, { stream: true });
        const lines = sseBuffer.split("\n");
        sseBuffer = lines.pop() || "";
        for (const line of lines) processLine(line);
      }
      sseBuffer += decoder.decode();
      if (sseBuffer) {
        for (const line of sseBuffer.split("\n")) processLine(line);
      }
      if (sseData) {
        try {
          const parsed = JSON.parse(sseData);
          if (sseEventType === "done") {
            const terminal = terminalOutcome(parsed, fullResponse);
            if (terminal !== undefined) {
              receivedTerminal = true;
              fullResponse = terminal;
            }
          }
        } catch { /* skip */ }
      }

      if (!receivedTerminal) fullResponse = OUTCOME_UNKNOWN;
      else if (fullResponse !== OUTCOME_UNKNOWN) {
        const scope = requestScopes.current[requestIdRef.current!];
        if (scope) forgetRequest(scope, requestIdRef.current!);
      }

      const finalCost = costInfo as { time_s: number; cost: number } | null;
      if (finalCost) setDeepCost(finalCost);

      setStreamingText("");
      if (fullResponse.trim()) {
        const costSuffix = finalCost
          ? `\n\n---\n*RLM: ${finalCost.time_s.toFixed(1)}s / $${finalCost.cost.toFixed(2)}*`
          : "";
        setMessages((prev) => [
          ...prev,
          {
            id: `asst-${Date.now()}`,
            role: "assistant",
            content: stripResidualMarkers(fullResponse).trim() + costSuffix,
            ...(fullResponse === OUTCOME_UNKNOWN ? { recovery: { requestId: requestIdRef.current!, agent, scope: requestScopes.current[requestIdRef.current!] } } : {}),
            timestamp: new Date(),
          },
        ]);
      }
    } catch (error) {
      setActivePlan(null);
      if ((error as Error).name !== "AbortError") {
        setMessages((prev) => [...prev, {
          id: `err-${Date.now()}`,
          role: "assistant",
          content: OUTCOME_UNKNOWN,
          recovery: { requestId: requestIdRef.current!, agent, scope: requestScopes.current[requestIdRef.current!] },
          timestamp: new Date(),
        }]);
      }
    } finally {
      abortRef.current = null;
      setIsPlanExecuting(false);
      setIsDeepReasoning(false);
      setDeepElapsed(0);
      setStreamingText("");
    }
  }, [activePlan, handleSharedStreamEvent, agent]);

  const handlePlanReject = useCallback(async () => {
    if (!activePlan) return;
    setPlanMode(false);
    try {
      await fetch("/api/chat/plan/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: activePlan.plan_id, ...(agent ? { agent } : {}) }),
      });
    } catch {
      // ignore
    }
    setActivePlan(null);
    setShowFeedbackInput(false);
    setPlanFeedback("");
  }, [activePlan, agent]);

  const handlePlanRevise = useCallback(async () => {
    if (!activePlan || !planFeedback.trim()) return;
    const feedback = planFeedback.trim();
    // Backend's implicit supersede clears the old plan when plan/start is called
    setActivePlan(null);
    setShowFeedbackInput(false);
    setPlanFeedback("");
    // Send feedback as the new message — history already has the previous plan
    sendPlanMessage(feedback);
  }, [activePlan, planFeedback, sendPlanMessage]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const handleAbort = async () => {
    if (isStopping) return;
    const stoppedRequest = requestIdRef.current;
    const stoppedController = abortRef.current;
    setIsStopping(true);
    setStopNotice("Stopping…");
    try {
      const response = await fetch("/api/chat/abort", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...(agent ? { agent } : {}), request_id: stoppedRequest }),
      });
      if (!response.ok) throw new Error("stop unconfirmed");
      const result = await response.json();
      if (!result.ok || !result.durable_stopped) throw new Error("stop unconfirmed");
      if (requestIdRef.current === stoppedRequest) {
        setStopNotice("Stop acknowledged. Already dispatched requests may finish; checking their recorded results.");
      }
      stoppedController?.abort();
    } catch {
      if (requestIdRef.current === stoppedRequest) {
        setStopNotice("Stop could not be confirmed. The request may still be running.");
      }
    } finally {
      if (requestIdRef.current === stoppedRequest) setIsStopping(false);
    }
  };

  const suggestedPrompts = [
    "How's everything running?",
    "Show my contacts",
    "Check the inbox",
    "What happened today?",
  ];

  return (
    <div className="h-full w-full flex flex-col bg-background" data-testid="chat-panel">
      {/* Header — richer on mobile since there's no app header above */}
      <div className={`px-4 border-b border-border flex items-center gap-2 ${mobile ? "py-3.5" : "py-3"}`}>
        <div className="relative">
          <div className="w-2 h-2 rounded-full bg-success" />
          <div className="absolute inset-0 w-2 h-2 rounded-full bg-success animate-ping opacity-40" />
        </div>
        <span className={`font-semibold ${mobile ? "text-base" : "text-sm"}`}>
          {agent ? currentAgentName : aiName}
        </span>
        {agentOptions.length > 1 && (
          <select
            value={agent}
            onChange={(event) => switchAgent(event.target.value)}
            disabled={isStreaming || isPlanExecuting || isPlanning || isDeepReasoning}
            aria-label="Which agent to talk to"
            className="max-w-[9rem] truncate rounded-md border border-border bg-background px-2 py-1 text-xs text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-50"
            data-testid="agent-switcher"
          >
            {agentOptions.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
        )}
        {agentOptions.length > 1 && defaultAgentMissing && (
          <span
            className="text-[10px] text-muted-foreground"
            title="Its manifest is missing or would not parse — check the fleet view."
            data-testid="agent-switcher-note"
          >
            {defaultAgent} is not in the fleet listing
          </span>
        )}
        {mobile && (
          <span className="text-xs text-muted-foreground ml-auto">Online</span>
        )}
        {planMode && !deepMode && (
          <Badge
            variant="outline"
            className={`border-warning/50 text-warning text-[10px] px-1.5 py-0 ${mobile ? "ml-auto" : ""}`}
            data-testid="plan-mode-badge"
          >
            Plan Mode
          </Badge>
        )}
        {deepMode && (
          <Badge
            variant="outline"
            className={`border-primary/50 text-primary text-[10px] px-1.5 py-0 ${mobile ? "ml-auto" : ""}`}
            data-testid="deep-mode-badge"
          >
            <Brain className="w-3 h-3 mr-1 inline" />
            Deep Plan
          </Badge>
        )}
        {isDeepReasoning && (
          <Badge
            variant="outline"
            className={`border-primary/50 text-primary text-[10px] px-1.5 py-0 animate-pulse ${mobile ? "ml-auto" : ""}`}
            data-testid="deep-reasoning-badge"
          >
            <Brain className="w-3 h-3 mr-1 inline" />
            Deep reasoning... {deepElapsed}s
          </Badge>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4">
        <div className="space-y-4" data-testid="message-list">
          {messages.length === 0 && !isStreaming && (
            <div className="space-y-4" data-testid="empty-state">
              <p className="text-sm text-muted-foreground">
                Ready when you are.
              </p>
              <div className="flex flex-wrap gap-2">
                {suggestedPrompts.map((prompt) => (
                  <button
                    key={prompt}
                    onClick={() => {
                      setInput(prompt);
                      setTimeout(() => inputRef.current?.focus(), 0);
                    }}
                    className="text-xs px-3 py-2 md:py-1.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors min-h-[44px] md:min-h-0"
                    data-testid="suggested-prompt"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              data-testid={`message-${msg.role}`}
            >
              <div
                className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
                  msg.role === "user"
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted border-l-2 border-l-primary"
                }`}
              >
                {msg.role === "assistant" ? (
                  <div className="prose prose-sm prose-invert max-w-none">
                    {msg.recovery ? (
                      <ChatRecovery request={msg.recovery} messageId={msg.id} onRecovered={recoverMessage} />
                    ) : (
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
                    )}
                  </div>
                ) : (
                  <p>{msg.content}</p>
                )}
                {msg.note && (
                  <p className="mt-1 text-[11px] opacity-75" data-testid="message-note">{msg.note}</p>
                )}
              </div>
            </div>
          ))}

          {/* The agent's own question — `approval_required` over the run's stream */}
          {/* `key={ask.id}` is load-bearing, not tidiness: without it React
              reuses one card instance across ids, and the previous decision's
              `settled`, status line and pinned deadline survive the swap — the
              next approval renders with its buttons already disabled and the
              old answer under it. That is exactly "the operator saw Answered
              and the agent sat blocked until its timeout", which is the failure
              this card was written to remove. */}
          {activeAsks.map((ask) => (
            <ChatAskCard
              key={ask.id}
              id={ask.id}
              kind={ask.kind}
              question={ask.question}
              options={ask.options}
              expiresAt={ask.expires_at}
              tool={ask.tool}
              timeoutSeconds={ask.timeout_seconds}
            />
          ))}

          {/* Plan approval card */}
          {activePlan && !isPlanExecuting && (
            <div className="flex justify-start" data-testid="plan-card">
              <div className={`max-w-[90%] rounded-lg border p-4 space-y-3 ${activePlan.deep_plan ? "border-primary/30 bg-primary/5" : "border-warning/30 bg-warning/5"}`}>
                <div className={`flex items-center gap-2 text-sm font-semibold ${activePlan.deep_plan ? "text-primary" : "text-warning"}`}>
                  {activePlan.deep_plan ? <Brain className="w-4 h-4" /> : <ClipboardList className="w-4 h-4" />}
                  <span>{activePlan.deep_plan ? "Deep Research Plan" : "Proposed Plan"}</span>
                </div>
                <div className="prose prose-sm prose-invert max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {activePlan.plan_text}
                  </ReactMarkdown>
                </div>
                <div className="flex gap-2 pt-1">
                  <Button
                    size="sm"
                    onClick={handlePlanApprove}
                    className="bg-success hover:bg-success/90 text-success-foreground"
                    data-testid="plan-approve"
                  >
                    <Check className="w-3.5 h-3.5 mr-1" />
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setShowFeedbackInput((prev) => !prev)}
                    className="text-warning hover:bg-warning/10"
                    data-testid="plan-edit"
                  >
                    <MessageSquareText className="w-3.5 h-3.5 mr-1" />
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={handlePlanReject}
                    className="text-destructive hover:bg-destructive/10"
                    data-testid="plan-reject"
                  >
                    <X className="w-3.5 h-3.5 mr-1" />
                    Reject
                  </Button>
                </div>
                {showFeedbackInput && (
                  <div className="space-y-2 pt-1" data-testid="plan-feedback-area">
                    <textarea
                      value={planFeedback}
                      onChange={(e) => setPlanFeedback(e.target.value)}
                      placeholder="What should change?"
                      className="w-full resize-none rounded-md border border-warning/30 bg-background px-3 py-2 text-sm min-h-[60px] focus:outline-none focus:ring-1 focus:ring-warning/50"
                      data-testid="plan-feedback-input"
                    />
                    <Button
                      size="sm"
                      onClick={handlePlanRevise}
                      disabled={!planFeedback.trim()}
                      className="bg-warning hover:bg-warning/90 text-warning-foreground"
                      data-testid="plan-revise"
                    >
                      Revise
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Streaming indicator */}
          {(isStreaming || isPlanExecuting || isPlanning) && (
            <div className="flex justify-start" data-testid="streaming-message">
              <div className={`max-w-[85%] rounded-lg px-3 py-2 text-sm bg-muted ${isPlanning ? "border border-warning/30" : ""}`}>
                {runtimeProgress && <p role="status" className="text-xs text-muted-foreground mb-2">{runtimeProgress}</p>}
                {isPlanning ? (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-warning" data-testid="planning-indicator">
                      <ClipboardList className="w-3.5 h-3.5 animate-pulse" />
                      <span className="text-xs font-medium">
                        {activeToolName ? `Checking ${activeToolName}...` : "Exploring..."}
                      </span>
                    </div>
                    {throttledStreamingText && (
                      <div className="prose prose-sm prose-invert max-w-none">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {throttledStreamingText}
                        </ReactMarkdown>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="space-y-1">
                    {currentIteration > 0 && maxIterations > 1 && (
                      <div className="flex items-center gap-2 text-xs text-muted-foreground" data-testid="step-progress">
                        <span>Step {currentIteration}/{maxIterations}</span>
                        <div className="flex-1 h-1 bg-background rounded-full overflow-hidden max-w-[100px]">
                          <div className="h-full bg-primary/50 rounded-full transition-all"
                               style={{ width: `${(currentIteration / maxIterations) * 100}%` }} />
                        </div>
                      </div>
                    )}
                    {throttledStreamingText ? (
                      <div className="prose prose-sm prose-invert max-w-none">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {throttledStreamingText}
                        </ReactMarkdown>
                      </div>
                    ) : activeToolName ? (
                      <div className="flex items-center gap-2 text-xs text-info" data-testid="tool-indicator">
                        <span className="inline-block w-2 h-2 rounded-full bg-info animate-pulse" />
                        <span>{activeToolName.replace(/_/g, " ")}</span>
                      </div>
                    ) : (
                      <div className="flex items-center gap-1.5 py-1">
                        <span className="typing-dot" />
                        <span className="typing-dot" />
                        <span className="typing-dot" />
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
          <div ref={scrollEndRef} />
        </div>
      </div>

      {/* Input area */}
      <div className="p-3 border-t border-border space-y-2">
        {/* Mode toggles — labeled chips on mobile, icon buttons on desktop */}
        {mobile ? (
          <div className="flex gap-2" data-testid="mobile-mode-toggles">
            <button
              onClick={() => {
                setPlanMode((prev) => !prev);
                setDeepMode(false);
                setDeepPlan(false);
                inputRef.current?.focus();
              }}
              disabled={isStreaming || isPlanExecuting || isPlanning || isDeepReasoning}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
                planMode && !deepMode
                  ? "bg-warning/15 text-warning border border-warning/30"
                  : "bg-muted/50 text-muted-foreground border border-transparent"
              } disabled:opacity-50`}
              data-testid="plan-toggle"
            >
              <ClipboardList className="w-3.5 h-3.5" />
              Plan
            </button>
            <button
              onClick={() => {
                setDeepMode((prev) => {
                  const next = !prev;
                  if (next) {
                    setPlanMode(true);
                    setDeepPlan(true);
                  } else {
                    setPlanMode(false);
                    setDeepPlan(false);
                  }
                  return next;
                });
                inputRef.current?.focus();
              }}
              disabled={isStreaming || isPlanExecuting || isPlanning || isDeepReasoning}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-colors ${
                deepMode
                  ? "bg-primary/15 text-primary border border-primary/30"
                  : "bg-muted/50 text-muted-foreground border border-transparent"
              } disabled:opacity-50`}
              data-testid="deep-toggle"
            >
              <Brain className="w-3.5 h-3.5" />
              Deep
            </button>
          </div>
        ) : null}

        <div className="flex items-end gap-2">
          {/* Desktop: icon buttons with tooltips */}
          {!mobile && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => {
                      setPlanMode((prev) => !prev);
                      setDeepMode(false);
                      setDeepPlan(false);
                      inputRef.current?.focus();
                    }}
                    className={planMode && !deepMode ? "text-warning bg-warning/10 hover:bg-warning/20" : planMode && deepMode ? "text-primary bg-primary/10 hover:bg-primary/20" : "text-muted-foreground hover:text-foreground"}
                    disabled={isStreaming || isPlanExecuting || isPlanning || isDeepReasoning}
                    data-testid="plan-toggle"
                  >
                    <ClipboardList className="w-4 h-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">
                  <p>Plan mode ({typeof navigator !== "undefined" && navigator?.platform?.includes("Mac") ? "⌘" : "Ctrl"}+Shift+P)</p>
                </TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => {
                      setDeepMode((prev) => {
                        const next = !prev;
                        if (next) {
                          setPlanMode(true);
                          setDeepPlan(true);
                        } else {
                          setPlanMode(false);
                          setDeepPlan(false);
                        }
                        return next;
                      });
                      inputRef.current?.focus();
                    }}
                    className={deepMode ? "text-primary bg-primary/10 hover:bg-primary/20" : "text-muted-foreground hover:text-foreground"}
                    disabled={isStreaming || isPlanExecuting || isPlanning || isDeepReasoning}
                    data-testid="deep-toggle"
                  >
                    <Brain className="w-4 h-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">
                  <p>Deep mode ({typeof navigator !== "undefined" && navigator?.platform?.includes("Mac") ? "⌘" : "Ctrl"}+Shift+D)</p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={deepMode ? "Ask a deep reasoning question..." : planMode ? "Describe what you want planned..." : isStreaming ? "Add to the running task..." : "Ask me anything..."}
            className={`flex-1 resize-none rounded-lg border bg-background px-3 py-2 text-sm min-h-[44px] max-h-[120px] focus:outline-none focus:ring-1 ${deepMode ? "border-primary/30 focus:ring-primary/50" : planMode ? "border-warning/30 focus:ring-warning/50" : "border-border focus:ring-ring"}`}
            rows={1}
            // Open while an ordinary reply streams: what is typed joins it.
            disabled={(isStreaming && (planMode || deepMode)) || isPlanExecuting || isPlanning || isDeepReasoning}
            data-testid="chat-input"
          />
          {stopNotice && <p role="status" className="text-xs text-muted-foreground">{stopNotice}</p>}
          {isStreaming && !planMode && !deepMode && input.trim() && (
            <Button
              size="icon"
              onClick={sendMessage}
              aria-label="Add to the running task"
              className="shrink-0"
              data-testid="followup-button"
            >
              <Send className="w-4 h-4" />
            </Button>
          )}
          {isStreaming || isPlanning || isDeepReasoning || isPlanExecuting ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={handleAbort}
              disabled={isStopping}
              aria-label={isStopping ? "Stopping request" : "Stop request"}
              className="shrink-0"
              data-testid="abort-button"
            >
              <Square className="w-4 h-4" />
            </Button>
          ) : (
            <Button
              size="icon"
              onClick={sendMessage}
              disabled={!input.trim() || isPlanExecuting}
              className="shrink-0"
              data-testid="send-button"
            >
              <Send className="w-4 h-4" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
