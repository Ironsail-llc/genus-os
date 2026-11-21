"use client";

import { useState, useRef, useEffect, useCallback, useMemo, useSyncExternalStore } from "react";
import { useRuntimeConfig } from "@/components/runtime-config";
import { reloadPage } from "@/lib/reload-page";
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
import { type ChatRecoveryRequest } from "@/components/chat-recovery";
import { ChatMarkdown } from "@/components/chat/chat-markdown";
import { MessageBubble } from "@/components/chat/message-bubble";
import { useMessageWindow } from "@/components/chat/use-message-window";
import { useHistoryPager } from "@/components/chat/use-history-pager";
import { useTurnActions } from "@/components/chat/use-turn-actions";
import { usePrependAnchor } from "@/components/chat/use-prepend-anchor";
import { historyToMessages } from "@/lib/chat/history-messages";
import { usePinnedScroll } from "@/components/chat/use-pinned-scroll";
import { useStableCallback } from "@/components/chat/use-stable-callback";
import { readChatStream, type ChatStreamData } from "@/lib/chat/read-chat-stream";
import { ChatAskCard, type ApprovalKind } from "@/components/chat-ask-card";
import { ChatLanding } from "@/components/chat/chat-landing";
import { ToolsPopover } from "@/components/chat/tools-popover";
import { AttachButton, NOT_IN_PLAN_OR_DEEP, NOT_WHILE_RUNNING, PendingAttachments, useChatAttachments } from "@/components/chat/chat-attachments";
import { attachmentKind, type AttachmentPayload, type AttachmentSummary } from "@/lib/chat/attachments";
import { steerRun, interruptRun, STEER_PLACEHOLDER, STEERED_NOTE } from "@/components/chat-steer";
import { repeatsQuickAction } from "@/components/business/quick-action-labels";
import {
  isKeyableAgentId,
  readStoredChatAgent,
  storeChatAgent,
} from "@/lib/chat/agent-session";
import { Send, Square, Check, X, ClipboardList, MessageSquareText, Brain, PanelRightClose } from "lucide-react";

import { forgetRequest, journalRequest, pendingRequests } from "@/lib/chat/request-journal";
// Types only: the outbox module itself is loaded when the panel is in a room
// (below), so the main conversation's first load never carries it.
import type { OutboxState, SendResult } from "@/lib/chat/room-outbox";
type RoomKit = typeof import("@/lib/chat/room-outbox");

import { OUTCOME_UNKNOWN, requestFailure, terminalOutcome } from "@/lib/chat/terminal-outcome";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** Absent for a history row whose time the engine did not send. */
  timestamp?: Date;
  /** The stored row id, for history pages. */
  serverId?: number;
  recovery?: ChatRecoveryRequest;
  /** Where a message sent while the agent was working went (live follow-ups). */
  note?: string;
  /** Files that went with a user turn. */
  attachments?: AttachmentSummary[];
  /** In a room: who wrote it, from history (a message typed here has none). */
  authorUserId?: string;
  authorName?: string;
  /** In a room: the edition this message announced. */
  editionId?: string;
  /** In a room: the outbox entry a stored message was sent from. */
  clientId?: string;
  /** In a room: refused before admission, so never matched to a stored row. */
  rejected?: boolean;
  /** In a room: the components this turn rendered, drawn under the reply. */
  renders?: RenderBlock[];
}

/** One `[RENDER:…]` marker or `render` event. */
interface RenderBlock {
  component: string;
  props: Record<string, unknown>;
}

/** A message typed while a turn ran. `seq` is typing order: queued ones go out in it. */
interface QueuedFollowup {
  id: string;
  text: string;
  seq: number;
  /**
   * The files that go with it. Only a message queued behind a busy room
   * carries any: it is sent later exactly as it was typed, files included.
   */
  files?: AttachmentPayload[];
}

const JOINED_NOTE = "Added to the running task";
const QUEUED_NOTE = "Queued — sends when this reply finishes";
const STOPPED_NOTE = "Not sent — the task was stopped";
const DROPPED_NOTE = "May not have reached the task — the connection dropped";

// A room's bubble notes and texts live in the room-outbox module, loaded on demand.
const NO_OUTBOX: OutboxState = { items: [], sending: null, blocked: false, answering: "", unsent: [] };
const noSubscription = () => () => {};
const READ_ONLY_NOTICES = {
  viewer: "You have view-only access to this workspace, so you can read it but not post.",
  archived: "This workspace is archived, so it can be read but not posted in.",
} as const;

/** Which room the panel is pointed at, when it is not the main conversation. */
export interface ChatRoomTarget {
  /** The room's id: sent as `room` on every request, never as a session key. */
  id: string;
  /** The user may read but not post: the composer is shut, and says why. */
  readOnly?: boolean;
  /** Why it is read-only: view-only access (the default), or an archived room. */
  readOnlyReason?: "viewer" | "archived";
  /**
   * Bumped by the room view on each live `room.message` / `room.edition`
   * event: the panel reads the room's newest messages again (and offers a
   * queued message again).
   */
  refreshKey?: number;
}

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
  /**
   * "page" when the panel is the chat home (shows the landing before the
   * first message); "dock" beside another view (a compact empty state).
   */
  variant?: "page" | "dock";
  /** The owner's first name, for the landing's greeting. */
  ownerName?: string;
  /** When true, the panel is docked beside another view and offers to collapse itself */
  docked?: boolean;
  /** Called by the header's collapse control; only used when `docked` */
  onCollapse?: () => void;
  /** A landing quick action opened its view (the shell decides where to show it). */
  onViewOpened?: () => void;
  /**
   * Point the panel at a room instead of the main conversation. Absent, the
   * panel is exactly what it always was. Present, it names the room by id on
   * every request, offers no agent switcher, plan or deep mode, names each
   * message's author, and draws the editions messages announced.
   */
  room?: ChatRoomTarget;
}

export function ChatPanel({
  mobile = false,
  variant = "dock",
  ownerName,
  docked = false,
  onCollapse,
  onViewOpened,
  room,
}: ChatPanelProps) {
  const roomId = room?.id;
  const roomReadOnly = Boolean(room?.readOnly);
  const roomRefreshKey = room?.refreshKey ?? 0;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const recoveryScopes = useRef<Record<string, string>>({});
  const requestScopes = useRef<Record<string, string>>({});
  // A long turn's stream can close before its `done` event; its reply then
  // arrives here from the recorded outcome. Its canvas markers must reach the
  // canvas exactly as a streamed reply's do, or a slow visual is silently lost.
  //
  // Not in a room. The visual state is the MAIN conversation's (one provider
  // wraps the shell): a workspace's charts are drawn in the workspace, under
  // the reply that rendered them, and nothing of its turns is handed to the
  // main canvas or its dashboard agent.
  const visualState = useVisualState();
  const notifyCanvas = visualState.notifyConversationUpdate;
  const renderOnCanvas = visualState.setRender;
  const messagesRef = useRef<ChatMessage[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  const recoverMessage = useCallback((id: string, text: string, plan?: ActivePlan) => {
    if (plan) setActivePlan(current => current ?? plan);
    setMessages((prev) => prev.map((item) => {
      if (item.id !== id) return item;
      if (item.recovery?.scope) forgetRequest(item.recovery.scope, item.recovery.requestId);
      return { ...item, content: stripResidualMarkers(text), recovery: undefined };
    }));

    const interceptor = new MarkerInterceptor();
    const first = interceptor.addChunk(text);
    const rest = interceptor.flush();
    const agentData: Record<string, unknown> = {};
    const roomRenders: RenderBlock[] = [];
    for (const marker of [...first.markers, ...rest.markers]) {
      if (marker.type === "dashboard" && marker.data && typeof marker.data === "object") {
        Object.assign(agentData, marker.data as Record<string, unknown>);
      } else if (marker.type === "render") {
        const block = { component: marker.component, props: marker.props as Record<string, unknown> };
        if (roomId) roomRenders.push(block);
        else renderOnCanvas(block);
      }
    }
    if (roomId) {
      if (roomRenders.length) {
        setMessages((prev) => prev.map((item) => (item.id === id ? { ...item, renders: roomRenders } : item)));
      }
      return;
    }
    const content = stripResidualMarkers(text).trim();
    const hasAgentData = Object.keys(agentData).length > 0;
    if (hasAgentData || content.length >= 200) {
      const prior = messagesRef.current;
      const at = prior.findIndex((m) => m.id === id);
      const question = prior.slice(0, at < 0 ? prior.length : at).reverse().find((m) => m.role === "user");
      notifyCanvas(
        [
          ...(question ? [{ role: "user", content: question.content }] : []),
          { role: "assistant", content },
        ].filter((m) => m.content.trim()),
        hasAgentData ? agentData : undefined,
      );
    }
  }, [notifyCanvas, renderOnCanvas, roomId]);
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
  // The listing answered 403: a member, on the default agent it may not name.
  const [listingRefused, setListingRefused] = useState(false);
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
  // Files go with an ordinary, fresh message only; any other time a paste or a
  // drop is refused inline, and switching into Plan or Deep drops what waits.
  const attachBlocked = planMode || deepMode ? NOT_IN_PLAN_OR_DEEP
    : isStreaming || isPlanExecuting || isPlanning || isDeepReasoning ? NOT_WHILE_RUNNING : null;
  const attachments = useChatAttachments(attachBlocked);
  const dropAttachments = attachments.drop;
  useEffect(() => {
    if (planMode || deepMode) dropAttachments("Plan and Deep mode do not take files.");
  }, [planMode, deepMode, dropAttachments]);
  const [deepElapsed, setDeepElapsed] = useState(0);
  const [, setDeepCost] = useState<{ time_s: number; cost: number } | null>(null);
  const throttledStreamingText = useThrottle(streamingText, 100);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Live follow-ups: messages typed while a turn runs. The engine takes them
  // into that turn (`followup_joined`) or hands them back (`followup_queued`,
  // `pending_followups`); those are sent as the next ordinary turn when the
  // running stream ends, so a message is delayed at worst, never dropped.
  const followupQueue = useRef<QueuedFollowup[]>([]);
  // Joined but not yet confirmed taken: the running stream's end settles them.
  const joinedFollowups = useRef<QueuedFollowup[]>([]);
  // Offered and awaiting the engine's answer; `pending_followups` can arrive first.
  const inFlightFollowups = useRef<QueuedFollowup[]>([]);
  const claimedFollowups = useRef<Set<string>>(new Set());
  const followupSeq = useRef(0);
  const [queuedCount, setQueuedCount] = useState(0);
  // In a room, a turn's renders are kept for its reply and nothing is
  // offered to the main canvas (see `visualState` above).
  const turnRenders = useRef<RenderBlock[]>([]);
  const setRender = useCallback(
    (block: { component: string; props: Record<string, unknown> }) => {
      if (roomId) turnRenders.current.push({ component: block.component, props: block.props });
      else visualState.setRender(block);
    },
    [roomId, visualState],
  );
  const notifyConversationUpdate = useCallback(
    (...args: Parameters<typeof visualState.notifyConversationUpdate>) => {
      if (!roomId) visualState.notifyConversationUpdate(...args);
    },
    [roomId, visualState],
  );
  const { aiName } = useRuntimeConfig();

  /** `?room=…` in a room, `?agent=…`, or nothing at all for the main agent. */
  const agentQuery = roomId
    ? `?room=${encodeURIComponent(roomId)}`
    : agent ? `?agent=${encodeURIComponent(agent)}` : "";
  /** The same choice for a request body: `{room}`, `{agent}`, or nothing. */
  const target = useMemo<{ room?: string; agent?: string }>(
    () => (roomId ? { room: roomId } : agent ? { agent } : {}),
    [roomId, agent],
  );
  /** What a recovery card needs to find this turn again. */
  const recoveryFor = useCallback(
    (requestId: string, scope: string | undefined): ChatRecoveryRequest => ({
      requestId, agent, scope, ...(roomId ? { room: roomId } : {}),
    }),
    [agent, roomId],
  );
  // Which chat's history has answered (or failed). The landing waits for it:
  // shown before the transcript arrives, it would flash and fire its requests
  // on every load of an existing conversation. A room's outbox waits for it too.
  const [historySettledFor, setHistorySettledFor] = useState<string | null>(null);

  // A room's messages leave through its outbox (`lib/chat/room-outbox.ts`):
  // one ordered queue, one sender, head first, one message per turn. It
  // outlives this panel, so what is queued survives leaving the workspace.
  const [roomKit, setRoomKit] = useState<RoomKit | null>(null);
  const [roomKitFailed, setRoomKitFailed] = useState(false);
  useEffect(() => {
    if (!roomId) return;
    let live = true;
    // Tried twice (a deploy can replace the chunk, the network can blink);
    // after that the panel says so instead of going quietly dead.
    const load = () => import("@/lib/chat/room-outbox");
    load()
      .catch(load)
      .then(
        (kit) => live && setRoomKit(kit),
        () => live && setRoomKitFailed(true),
      );
    return () => {
      live = false;
    };
  }, [roomId]);
  const outbox = useMemo(() => (roomId && roomKit ? roomKit.roomOutbox(roomId) : null), [roomId, roomKit]);
  const outboxState = useSyncExternalStore(
    outbox ? outbox.subscribe : noSubscription,
    outbox ? outbox.state : () => NO_OUTBOX,
    () => NO_OUTBOX,
  );
  // Whom the room's agent is answering while our head waits (refused as busy).
  const roomBusy = outboxState.blocked ? outboxState.answering : null;
  const [roomHistoryFailed, setRoomHistoryFailed] = useState(false);

  // The outbox entries this panel has itself offered: the only in-flight
  // send it may draw a bubble for (see `pendingBubbles`).
  const startedHere = useRef(new Set<string>());
  /** Bubbles, keyed by client id, for outbox entries not drawn (after leaving and coming back). */
  // Also draws, as not sent, what failed after the panel that offered it had gone.
  const withOutbox = useCallback((list: ChatMessage[], state = outbox?.state()): ChatMessage[] => {
    if (!state || !roomKit) return list;
    const waiting = roomKit.outboxBubbles(list, state, startedHere.current);
    return waiting.length ? [...list, ...waiting] : list;
  }, [outbox, roomKit]);
  // The history load reads it through a ref: the outbox module arriving must
  // not re-run that load, which would clear what is already drawn.
  const withOutboxRef = useRef(withOutbox);
  withOutboxRef.current = withOutbox;

  // Each waiting bubble says where it stands: queued, queued behind whom, or
  // (the head, while it is sent) nothing. And an entry that now needs a bubble
  // (another panel's send, refused after this one opened) gets one.
  const outboxHistoryReady = historySettledFor === agentQuery;
  useEffect(() => {
    if (!outbox || !roomKit) return;
    if (outboxHistoryReady) {
      setMessages((prev) => withOutbox(prev, outboxState));
      // A failed send is acknowledged only once it is on screen (drawn, or as
      // its stored copy): told once, never forgotten unseen.
      outbox.acknowledge(roomKit.unsentShown(outboxState, messages));
    }
    setMessages((prev) => {
      const notes = new Map(outboxState.items.map((item) => [item.id, roomKit.noteFor(item, outboxState)]));
      let changed = false;
      const next = prev.map((m) => {
        if (!notes.has(m.id) || m.note === notes.get(m.id)) return m;
        changed = true;
        return { ...m, note: notes.get(m.id) };
      });
      return changed ? next : prev;
    });
  }, [outbox, roomKit, outboxState, outboxHistoryReady, withOutbox, messages]);

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

  // Follow the bottom while the reader is there (throttled during streaming);
  // sending a message brings them back to it. Keyed on the NEWEST user
  // message, not a count: an older page from "Load earlier" adds user turns
  // above the reader and must not throw them to the bottom.
  const lastSentId = useMemo(() => messages.findLast((m) => m.role === "user")?.id ?? "", [messages]);
  const { containerRef: scrollRef, onScroll: onPinnedScroll } = usePinnedScroll<HTMLDivElement>(
    [messages, throttledStreamingText, activePlan, isDeepReasoning],
    lastSentId,
  );
  const { visible: visibleMessages, hiddenCount, showEarlier } = useMessageWindow(messages);
  const rememberScroll = usePrependAnchor(scrollRef, visibleMessages[0]?.id, visibleMessages.at(-1)?.id);
  const onListScroll = useCallback(() => {
    onPinnedScroll();
    rememberScroll();
  }, [onPinnedScroll, rememberScroll]);
  const onBubbleRecovered = useStableCallback(recoverMessage);
  const historyPager = useHistoryPager(agentQuery, messages, setMessages);
  const settleHistoryPages = historyPager.settle;

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
    // A room's agent is the room's: no switcher, and no remembered agent adopted.
    if (roomId) return;
    let cancelled = false;
    fetch("/api/bridge/api/agent-manifests")
      .then((res) => {
        if (!cancelled && res.status === 403) setListingRefused(true);
        return res.ok ? res.json() : null;
      })
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
  }, [roomId]);

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
          const loaded: ChatMessage[] = historyToMessages(data.messages);
          const restored: ChatMessage[] = scope ? pendingRequests(scope)
            .filter((requestId) => requestId !== requestIdRef.current)
            .map((requestId) => ({
              id: `recovery-${requestId}`, role: "assistant", content: OUTCOME_UNKNOWN,
              timestamp: new Date(), recovery: recoveryFor(requestId, scope),
            })) : [];
          setMessages(withOutboxRef.current([...loaded, ...restored]));
          settleHistoryPages(data.hasMore === true);
          setHistorySettledFor(agentQuery);
          setRoomHistoryFailed(false);
        }
      })
      .catch(() => {
        // Engine not available on mount. The conversation is unknown, not
        // known to be long, so the landing may show. In a room it is said:
        // the route refused (the engine did not confirm the room) or failed.
        if (cancelled) return;
        setHistorySettledFor(agentQuery);
        setRoomHistoryFailed(true);
        setMessages((prev) => withOutboxRef.current(prev));
      });
    return () => {
      cancelled = true;
    };
  }, [agentQuery, agent, settleHistoryPages, recoveryFor]);

  // A room is shared: other members' messages, and the agent's answers to
  // them, land while this panel is open. On each live room event (the room
  // view bumps `refreshKey`) the newest page is read again and what is not on
  // screen yet is added. A message typed here is shown before it is stored, so
  // its stored copy replaces it (matched by role and text) instead of
  // appearing twice. Deferred while a turn of ours runs; the stream is
  // authoritative then, and the next bump catches up.
  const caughtUpTo = useRef(roomRefreshKey);
  const panelBusy = isStreaming || isPlanning || isPlanExecuting || isDeepReasoning;
  useEffect(() => {
    if (!roomId || !roomKit || panelBusy || roomRefreshKey === caughtUpTo.current) return;
    caughtUpTo.current = roomRefreshKey;
    let cancelled = false;
    fetch(`/api/chat/history${agentQuery}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data) return;
        const stored = historyToMessages(data.messages).filter((m) => m.serverId !== undefined);
        // A stored copy replaces its local bubble (by client id), never joins it.
        setMessages((prev) => withOutbox(roomKit.mergeRoomRows(prev, stored)));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [roomId, roomKit, roomRefreshKey, panelBusy, agentQuery, withOutbox]);

  // Recover pending plan on page refresh
  //
  // Cancelled for a sharper reason than the transcript: a plan recovered from
  // the ABANDONED agent's session would become `activePlan` while `agent` is
  // somebody else, and Approve would then post that plan_id with the new
  // agent's key — executing a plan against a session that never saw it, which
  // is the thing threading the key through plan/approve exists to prevent.
  useEffect(() => {
    // Plan and Deep are main-conversation modes; a room offers neither.
    if (roomId) return;
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
  }, [agentQuery, agent, roomId]);

  // Keyboard shortcut: Ctrl/Cmd+Shift+P toggles plan mode, Ctrl/Cmd+Shift+D toggles deep+plan
  useEffect(() => {
    if (roomId) return;
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
  }, [roomId]);

  // Recover active deep reasoning on page refresh
  useEffect(() => {
    if (roomId) return;
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
  }, [agentQuery, agent, roomId]);

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
      const scope = await journalRequest(agent, requestId, recoveryScopes.current[agent], controller.signal, roomId);
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

      let fullResponse = "";
      let gotPlanEvent = false;
      let receivedTerminal = false;

      const handleSSEEvent = (eventType: string, parsed: ChatStreamData) => {
        try {
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

      for await (const { event, data } of readChatStream(res)) handleSSEEvent(event, data);

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
        ...(fullResponse === OUTCOME_UNKNOWN ? { recovery: recoveryFor(requestIdRef.current!, requestScopes.current[requestIdRef.current!]) } : {}),
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
  }, [input, isStreaming, isPlanning, deepPlan, handleSharedStreamEvent, agent, roomId, recoveryFor]);

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
    if (isStopping) return; // a stop is under way: what is typed now stays in the box
    const item = { id: `user-${Date.now()}`, text, seq: followupSeq.current++ };
    setMessages((prev) => [...prev, { id: item.id, role: "user", content: text, timestamp: new Date(), note: "Sending…" }]);
    setInput("");
    inFlightFollowups.current = [...inFlightFollowups.current, item];
    if (await steerRun(text, agent, requestIdRef.current, roomId)) {
      inFlightFollowups.current = inFlightFollowups.current.filter((f) => f.id !== item.id);
      noteMessages([item.id], STEERED_NOTE);
      return;
    }
    let joined = false;
    try {
      const res = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // It names the turn it is for: two tabs are two turns, two inboxes.
        body: JSON.stringify({ message: text, join_running: true, running_request_id: requestIdRef.current, ...target }),
      });
      joined = res.ok && (await res.text()).includes("event: followup_joined");
    } catch {
      joined = false;
    }
    inFlightFollowups.current = inFlightFollowups.current.filter((f) => f.id !== item.id);
    if (claimedFollowups.current.delete(item.id)) return; // the stream already handed it back
    if (joined) {
      joinedFollowups.current = [...joinedFollowups.current, item];
      noteMessages([item.id], JOINED_NOTE);
    } else {
      queueFollowups([item]);
    }
  }, [agent, roomId, target, isStopping, noteMessages, queueFollowups]);

  /** Stop: what was waiting goes with the task, not out as a turn after it. */
  const discardFollowups = useCallback(() => {
    const ids = [...followupQueue.current, ...joinedFollowups.current, ...inFlightFollowups.current].map((f) => f.id);
    ids.forEach((id) => claimedFollowups.current.add(id));
    followupQueue.current = [];
    joinedFollowups.current = [];
    setQueuedCount(0);
    noteMessages(ids, STOPPED_NOTE);
  }, [noteMessages]);

  // `resend` is Regenerate's text: sent as a new turn, the composer left alone.
  const sendMessage = useCallback(async (queued?: QueuedFollowup[] | unknown, resend?: string) => {
    const flushing = Array.isArray(queued) ? (queued as QueuedFollowup[]) : null;
    if (!flushing && (deepMode || planMode)) {
      // Deep mode routes through plan mode (deep always plans first).
      // Standalone plan mode also uses sendPlanMessage.
      return sendPlanMessage(resend);
    }

    const text = flushing ? [...flushing].sort((a, b) => a.seq - b.seq).map((q) => q.text).join("\n\n") : (resend ?? input).trim();
    if (roomId && !flushing) {
      // Never the main conversation's path: until the outbox has loaded (a
      // moment, on first opening a room) what was typed stays in the composer.
      if (!outbox || !roomKit) return;
      // In a room every message, typed or regenerated, takes its place in the
      // outbox now, in typing order, with its files; the outbox's one sender
      // offers it (back through this function, as a flush) when its turn
      // comes. Nothing here sends it, steers with it or joins a turn with it.
      const queuedFiles = resend !== undefined ? [] : attachments.files.map(({ name, mime, size, data }) => ({ name, mime, size, data }));
      if (!text && !queuedFiles.length) return;
      if (resend === undefined) {
        setInput("");
        attachments.clear();
      }
      setActiveAsks([]);
      const item = outbox.enqueue(text, queuedFiles);
      const bubble = roomKit.bubbleFor(item, roomKit.noteFor(item, outbox.state()));
      setMessages((prev) => (prev.some((m) => m.id === item.id) ? prev : [...prev, bubble]));
      return;
    }
    if (!flushing && !resend && text && isStreaming) return sendFollowup(text);
    // Files go with a fresh turn only: never with a flushed follow-up, and not
    // with a Regenerate (the panel keeps only their summary, not their bytes).
    // A message queued behind a busy room is the exception: it carries its own.
    const files = flushing ? flushing.flatMap((q) => q.files ?? [])
      : resend !== undefined ? [] : attachments.files.map(({ name, mime, size, data }) => ({ name, mime, size, data }));
    // The outbox sends one room message at a time, so a room flush does not
    // consult `isStreaming`: it can still read true from the turn that just
    // ended, before React rendered its end.
    if (!text && !files.length) return roomId ? ({ outcome: "failed", rejected: true } as SendResult) : undefined;
    if (isStreaming && !(roomId && flushing)) return;

    setActiveAsks([]);

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: text,
      timestamp: new Date(),
      ...(files.length ? { attachments: files.map((f) => ({ name: f.name, size: f.size, kind: attachmentKind(f.mime) })) } : {}),
    };
    if (flushing) {
      // Their bubbles are already on screen, where they were typed.
      noteMessages(flushing.map((q) => q.id), undefined);
    } else {
      setMessages((prev) => [...prev, userMsg]);
      if (resend === undefined) {
        setInput("");
        attachments.clear();
      }
    }
    setIsStreaming(true);
    setStreamingText("");
    turnRenders.current = [];

    const controller = new AbortController();
    abortRef.current = controller;
    requestIdRef.current = crypto.randomUUID();
    setStopNotice(null);
    setIsStopping(false);
    setRuntimeProgress("Submitting request…");
    let streamSettled = false;
    // Whether the request may have reached the server: after this, a failure's outcome is unknown.
    let dispatched = false;

    try {
      const requestId = requestIdRef.current!;
      const scope = await journalRequest(agent, requestId, recoveryScopes.current[agent], controller.signal, roomId);
      if (scope) requestScopes.current[requestId] = scope;
      if (controller.signal.aborted) {
        if (scope) forgetRequest(scope, requestId);
        controller.signal.throwIfAborted();
      }
      dispatched = true;
      const res = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text, request_id: requestIdRef.current, ...target, ...(files.length ? { attachments: files } : {}),
          // A room message names its outbox entry, so its stored copy can be matched to its bubble.
          ...(roomId && flushing?.length === 1 ? { client_id: flushing[0].id } : {}),
        }),
        signal: controller.signal,
      });

      if (roomId && res.status === 409) {
        // Another member's turn holds the room. Nothing was admitted: what was
        // typed stays where it was typed, marked queued, and is offered again
        // when the room moves on (a live message) or after a short wait.
        const busy = await res.clone().json().catch(() => null);
        if (busy?.error === "room_busy") {
          // The outbox keeps it at the head and offers it again.
          const scope = requestScopes.current[requestIdRef.current!];
          if (scope) forgetRequest(scope, requestIdRef.current!);
          setIsStreaming(false);
          setStreamingText("");
          setRuntimeProgress(null);
          streamSettled = true;
          return { outcome: "busy", answering: typeof busy.answering === "string" ? busy.answering : "" } as SendResult;
        }
      }

      if (!res.ok || !res.body) {
        const failure = await requestFailure(res);
        const scope = requestScopes.current[requestIdRef.current!];
        if (failure.rejected && scope) forgetRequest(scope, requestIdRef.current!);
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`, role: "assistant",
          content: failure.text, timestamp: new Date(),
          ...(!failure.rejected ? { recovery: recoveryFor(requestIdRef.current!, scope) } : {}),
        };
        setMessages((prev) => [...prev, errorMsg]);
        setIsStreaming(false);
        setStreamingText("");
        setActiveToolName(null);
        setCurrentIteration(0);
        setMaxIterations(0);
        return roomId ? ({ outcome: "failed", rejected: failure.rejected } as SendResult) : undefined;
      }

      let fullResponse = "";
      let receivedTerminal = false;
      const collectedAgentData: Record<string, unknown> = {};

      const interceptor = new MarkerInterceptor();

      const handleSSEEvent = (eventType: string, parsed: ChatStreamData) => {
        try {
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
            const orphans: ChatMessage[] = [];
            const items = pending.map((text, n) => {
              const take = (list: QueuedFollowup[]) => {
                const at = list.findIndex((j) => j.text === text);
                return at >= 0 ? list.splice(at, 1)[0] : null;
              };
              const inFlight = take(inFlightFollowups.current);
              if (inFlight) claimedFollowups.current.add(inFlight.id);
              const match = take(joinedFollowups.current) ?? inFlight;
              if (match) return match;
              // Not typed here (another tab's, say): show it before sending it.
              const orphan = { id: `user-${Date.now()}-${n}`, text, seq: followupSeq.current++ };
              orphans.push({ id: orphan.id, role: "user", content: text, timestamp: new Date() });
              return orphan;
            });
            if (orphans.length) setMessages((prev) => [...prev, ...orphans]);
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

      for await (const { event, data } of readChatStream(res)) handleSSEEvent(event, data);

      // Clear streaming state BEFORE adding the message to avoid duplicate display
      setIsStreaming(false);
      setStreamingText("");
      setActiveToolName(null);
      setCurrentIteration(0);
      setMaxIterations(0);

      streamSettled = receivedTerminal;
      if (!receivedTerminal) fullResponse = OUTCOME_UNKNOWN;
      else if (fullResponse !== OUTCOME_UNKNOWN) {
        const scope = requestScopes.current[requestIdRef.current!];
        if (scope) forgetRequest(scope, requestIdRef.current!);
      }

      // Finalize the message
      const renders = roomId && turnRenders.current.length ? turnRenders.current : undefined;
      turnRenders.current = [];
      const assistantMsg: ChatMessage = {
        id: `asst-${Date.now()}`,
        role: "assistant",
        content: stripResidualMarkers(fullResponse).trim(),
        ...(renders ? { renders } : {}),
        ...(fullResponse === OUTCOME_UNKNOWN ? { recovery: recoveryFor(requestIdRef.current!, requestScopes.current[requestIdRef.current!]) } : {}),
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
      // Admitted: even a reply lost on the way is not sent again.
      return roomId ? ({ outcome: "ok" } as SendResult) : undefined;
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`,
          role: "assistant",
          content: OUTCOME_UNKNOWN,
          recovery: recoveryFor(requestIdRef.current!, requestScopes.current[requestIdRef.current!]),
          timestamp: new Date(),
        };
        setMessages((prev) => [...prev, errorMsg]);
      }
      // Possibly dispatched (its recovery card says so), or stopped: never resent.
      return roomId ? ({ outcome: "failed", rejected: !dispatched } as SendResult) : undefined;
    } finally {
      setIsStreaming(false);
      setStreamingText("");
      abortRef.current = null;
      // A stream that ended without its terminal event never said whether the
      // task took what joined it: say so rather than keep claiming "Added".
      if (!streamSettled) noteMessages(joinedFollowups.current.map((f) => f.id), DROPPED_NOTE);
      joinedFollowups.current = [];
    }
  }, [input, attachments, isStreaming, planMode, deepMode, sendPlanMessage, sendFollowup, noteMessages, queueFollowups, notifyConversationUpdate, setRender, handleSharedStreamEvent, agent, roomId, target, recoveryFor, outbox, roomKit]);

  // Another agent is another conversation: what waited for this one is not sent there.
  useEffect(() => {
    followupQueue.current = [];
    joinedFollowups.current = [];
    setQueuedCount(0);
  }, [agent]);

  // Once nothing is running, what waited goes out as one ordinary turn.
  const sendMessageRef = useRef(sendMessage);
  sendMessageRef.current = sendMessage;
  useEffect(() => {
    if (!queuedCount || isStreaming || isPlanning || isPlanExecuting || isDeepReasoning) return;
    const items = followupQueue.current;
    followupQueue.current = [];
    setQueuedCount(0);
    if (outbox) {
      // A room never joins or steers a turn, so nothing lands here from this
      // panel; anything the engine hands back still leaves through the outbox,
      // in order, its bubble taking the outbox item's id.
      for (const queued of [...items].sort((a, b) => a.seq - b.seq)) {
        const item = outbox.enqueue(queued.text, queued.files);
        setMessages((prev) => prev.map((m) => (m.id === queued.id ? { ...m, id: item.id } : m)));
      }
      return;
    }
    void sendMessageRef.current(items);
  }, [queuedCount, isStreaming, isPlanning, isPlanExecuting, isDeepReasoning, outbox]);

  // The panel is the room outbox's sender while it is mounted and its history
  // has loaded (so a reply never lands before the transcript it follows). It
  // sends a message as a flush: the bubble is already on screen.
  const historyReady = historySettledFor === agentQuery;
  useEffect(() => {
    if (!outbox || !historyReady) return;
    return outbox.attach(async (item) => {
      startedHere.current.add(item.id);
      const result = (await sendMessageRef.current([item])) ?? { outcome: "failed" };
      // Refused here: marked, so no stored row is ever taken for it.
      setMessages((prev) => roomKit!.markRefused(prev, item.id, result));
      return result;
    });
  }, [outbox, roomKit, historyReady]);
  // A live room message means the room moved on: a refused head is offered again now.
  const lastRefreshKey = useRef(roomRefreshKey);
  useEffect(() => {
    if (roomRefreshKey === lastRefreshKey.current) return;
    lastRefreshKey.current = roomRefreshKey;
    outbox?.roomFreed();
  }, [roomRefreshKey, outbox]);

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
      const scope = await journalRequest(agent, requestId, recoveryScopes.current[agent], controller.signal, roomId);
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
          ...(!failure.rejected ? { recovery: recoveryFor(requestIdRef.current!, scope) } : {}),
        }]);
        return;
      }

      // Stream the execution response. A bare event (no `event:` line) was never
      // a delta on this stream.
      let fullResponse = "";
      let receivedTerminal = false;
      let costInfo: { time_s: number; cost: number } | null = null;

      const handleSSEEvent = (eventType: string, parsed: ChatStreamData) => {
        try {
          if (handleSharedStreamEvent(eventType, parsed)) return;
          if (eventType === "delta") {
            fullResponse += parsed.text || "";
            setStreamingText(fullResponse);
          } else if (eventType === "deep_progress") {
            setDeepElapsed(parsed.elapsed_s || 0);
          } else if (eventType === "deep_result") {
            fullResponse = parsed.response || fullResponse;
            costInfo = {
              time_s: parsed.execution_time_s || 0,
              cost: parsed.cost_usd || 0,
            };
          } else if (eventType === "done") {
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
          } else if (eventType === "error") {
            fullResponse = `Error: ${parsed.error || "Unknown error"}`;
          }
        } catch { /* skip */ }
      };

      for await (const { event, data } of readChatStream(res, { defaultEvent: "" })) handleSSEEvent(event, data);

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
            ...(fullResponse === OUTCOME_UNKNOWN ? { recovery: recoveryFor(requestIdRef.current!, requestScopes.current[requestIdRef.current!]) } : {}),
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
          recovery: recoveryFor(requestIdRef.current!, requestScopes.current[requestIdRef.current!]),
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
  }, [activePlan, handleSharedStreamEvent, agent, roomId, recoveryFor]);

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

  const busy = isStreaming || isPlanExecuting || isPlanning || isDeepReasoning;
  const { regenerate, edit: editMessage } = useTurnActions({
    messages,
    setMessages,
    busy,
    resend: (text) => void sendMessage(undefined, text),
    compose: (text) => {
      setInput(text);
      setTimeout(() => inputRef.current?.focus(), 0);
    },
  });

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const handleAbort = async () => {
    if (isStopping) return;
    discardFollowups();
    if (isStreaming && (await interruptRun(agent, requestIdRef.current, roomId))) {
      setStopNotice("Stopping the run…");
      return;
    }
    const stoppedRequest = requestIdRef.current;
    const stoppedController = abortRef.current;
    setIsStopping(true);
    setStopNotice("Stopping…");
    try {
      const response = await fetch("/api/chat/abort", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...target, request_id: stoppedRequest }),
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

  // The landing shows the quick actions beside the prompts; a prompt that says
  // the same thing as one of them would be a second button with one name.
  const landingPrompts = suggestedPrompts.filter((prompt) => !repeatsQuickAction(prompt));

  // A suggested prompt fills the composer; it is never sent on its own.
  const applyPrompt = (prompt: string) => {
    setInput(prompt);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  return (
    <div className="h-full w-full flex flex-col bg-background" data-testid="chat-panel">
      {/* Header — richer on mobile since there's no app header above. A room
          view draws its own header above the panel. */}
      {!roomId && (
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
        {docked && (
          <button
            type="button"
            onClick={onCollapse}
            aria-label="Collapse chat panel"
            title="Collapse chat panel"
            className="ml-auto rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            data-testid="chat-dock-close"
          >
            <PanelRightClose className="size-4" />
          </button>
        )}
      </div>
      )}

      {/* Messages */}
      <div ref={scrollRef} onScroll={onListScroll} className="flex-1 overflow-y-auto p-4">
        <div className="space-y-4" data-testid="message-list">
          {roomId && roomHistoryFailed && (
            <p className="py-6 text-center text-sm text-muted-foreground" data-testid="room-chat-unavailable">
              This workspace&apos;s conversation could not be loaded. Nothing has been sent.
            </p>
          )}
          {roomId && !roomHistoryFailed && messages.length === 0 && !isStreaming && historySettledFor === agentQuery && (
            <p className="py-6 text-center text-sm text-muted-foreground" data-testid="room-chat-empty">
              No messages yet.{roomReadOnly ? "" : " Ask the agent about this workspace."}
            </p>
          )}
          {!roomId && messages.length === 0 && !isStreaming && variant === "page" && historySettledFor === agentQuery && (
            <ChatLanding
              ownerName={ownerName}
              suggestedPrompts={landingPrompts}
              onPrompt={applyPrompt}
              onViewOpened={onViewOpened}
            />
          )}
          {!roomId && messages.length === 0 && !isStreaming && variant === "dock" && (
            <div className="space-y-4" data-testid="empty-state">
              <p className="text-sm text-muted-foreground">
                Ready when you are.
              </p>
              <div className="flex flex-wrap gap-2">
                {suggestedPrompts.map((prompt) => (
                  <button
                    key={prompt}
                    onClick={() => applyPrompt(prompt)}
                    className="text-xs px-3 py-2 md:py-1.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors min-h-[44px] md:min-h-0"
                    data-testid="suggested-prompt"
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          )}

          {hiddenCount > 0 && (
            <div className="flex justify-center">
              <button
                type="button"
                onClick={showEarlier}
                className="text-xs text-muted-foreground hover:text-foreground"
                data-testid="show-earlier"
              >
                Show {hiddenCount} earlier {hiddenCount === 1 ? "message" : "messages"}
              </button>
            </div>
          )}
          {hiddenCount === 0 && historyPager.hasMore && (
            <div className="flex justify-center">
              <button
                type="button"
                onClick={async () => {
                  // Past the render window the page would land behind "Show earlier".
                  if ((await historyPager.loadEarlier()) > 0) showEarlier();
                }}
                disabled={historyPager.loading}
                className="text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
                data-testid="load-earlier"
              >
                {historyPager.loading ? "Loading earlier messages…" : "Load earlier messages"}
              </button>
            </div>
          )}
          {visibleMessages.map((msg) => (
            <MessageBubble
              key={msg.id}
              msg={msg}
              onRecovered={onBubbleRecovered}
              busy={busy}
              onRegenerate={roomReadOnly ? undefined : regenerate}
              onEdit={roomReadOnly ? undefined : editMessage}
              roomId={roomId}
            />
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
                <ChatMarkdown content={activePlan.plan_text} />
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
                      <ChatMarkdown content={throttledStreamingText} />
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
                      <ChatMarkdown content={throttledStreamingText} />
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
        </div>
      </div>

      {/* Input area — files can be dropped anywhere on it */}
      <div
        className="p-3 border-t border-border space-y-2"
        onDragOver={(e) => { if (e.dataTransfer?.types?.includes?.("Files")) e.preventDefault(); }}
        onDrop={(e) => { if (e.dataTransfer?.files?.length) { e.preventDefault(); void attachments.add(e.dataTransfer.files); } }}
        data-testid="chat-composer"
      >
        <PendingAttachments files={attachments.files} refusal={attachments.refusal} onRemove={attachments.remove} />
        {roomKitFailed && (
          <p role="alert" className="text-xs text-destructive" data-testid="room-chat-load-failed">
            Couldn&apos;t load this workspace&apos;s chat.{" "}
            <button type="button" onClick={reloadPage} className="underline">Reload</button>
          </p>
        )}
        {roomReadOnly && (
          <p id="room-readonly-notice" className="text-xs text-muted-foreground" data-testid="room-readonly-notice">
            {READ_ONLY_NOTICES[room?.readOnlyReason === "archived" ? "archived" : "viewer"]}
          </p>
        )}
        {roomBusy !== null && roomKit && (
          <p role="status" className="text-xs text-muted-foreground" data-testid="room-busy-notice">
            {roomKit.busyNotice(roomBusy)}
          </p>
        )}
        {/* Mode toggles — labeled chips on mobile, icon buttons on desktop */}
        {mobile && !roomId ? (
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
              aria-label="Plan mode"
              aria-pressed={planMode && !deepMode}
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
              aria-label="Deep mode"
              aria-pressed={deepMode}
              data-testid="deep-toggle"
            >
              <Brain className="w-3.5 h-3.5" />
              Deep
            </button>
            <ToolsPopover agentId={agent || defaultAgent || null} listingRefused={listingRefused} mobile />
          </div>
        ) : null}

        <div className="flex items-end gap-2">
          {/* Desktop: icon buttons with tooltips. A room offers no Plan or Deep. */}
          {!mobile && !roomId && (
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
                    aria-label="Plan mode"
                    aria-pressed={planMode && !deepMode}
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
                    aria-label="Deep mode"
                    aria-pressed={deepMode}
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
          {!mobile && !roomId && (
            <ToolsPopover agentId={agent || defaultAgent || null} listingRefused={listingRefused} />
          )}
          <AttachButton
            onFiles={(files) => void attachments.add(files)}
            disabled={attachBlocked !== null || roomReadOnly}
          />
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            // Files only when the paste carries no text: Word, Excel and OneNote put a picture beside it.
            onPaste={(e) => { const c = e.clipboardData; if (c?.files?.length && !Array.from(c.types ?? []).includes("text/plain")) { e.preventDefault(); void attachments.add(c.files); } }}
            placeholder={deepMode ? "Ask a deep reasoning question..." : planMode ? "Describe what you want planned..." : isStreaming ? STEER_PLACEHOLDER : "Ask me anything..."}
            className={`flex-1 resize-none rounded-lg border bg-background px-3 py-2 text-sm min-h-[44px] max-h-[120px] focus:outline-none focus:ring-1 ${deepMode ? "border-primary/30 focus:ring-primary/50" : planMode ? "border-warning/30 focus:ring-warning/50" : "border-border focus:ring-ring"}`}
            rows={1}
            // Open while an ordinary reply streams: what is typed joins it.
            disabled={roomReadOnly || roomKitFailed || (isStreaming && (planMode || deepMode)) || isPlanExecuting || isPlanning || isDeepReasoning}
            aria-describedby={roomReadOnly ? "room-readonly-notice" : undefined}
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
              disabled={roomReadOnly || roomKitFailed || (!input.trim() && !attachments.files.length) || isPlanExecuting}
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
