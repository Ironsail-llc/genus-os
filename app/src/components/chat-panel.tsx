"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
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
import { Send, Square, Check, X, ClipboardList, MessageSquareText } from "lucide-react";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

interface ActivePlan {
  plan_id: string;
  plan_text: string;
  original_message: string;
  status: string;
}

/** Strip any residual markers from messages (history or live).
 *  Handles both bracketed [RENDER:...] and un-bracketed RENDER:... forms
 *  since the agent sometimes omits the opening bracket. */
function stripResidualMarkers(text: string): string {
  return text
    .replace(/\[DASHBOARD:\{[^]*?\}\]/g, "")
    .replace(/\[RENDER:[a-z_]+:[^]*?\]/g, "")
    // Un-bracketed variants (agent sometimes omits the opening [)
    .replace(/\bRENDER:[a-z_]+:\{[^]*?\}\]?/g, "")
    .replace(/\bDASHBOARD:\{[^]*?\}\]?/g, "")
    // Strip plan markers
    .replace(/\[PLAN_READY\]/g, "")
    .trim();
}

export function ChatPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [activePlan, setActivePlan] = useState<ActivePlan | null>(null);
  const [isPlanExecuting, setIsPlanExecuting] = useState(false);
  const [planMode, setPlanMode] = useState(false);
  const [isPlanning, setIsPlanning] = useState(false);
  const [showFeedbackInput, setShowFeedbackInput] = useState(false);
  const [planFeedback, setPlanFeedback] = useState("");
  const [activeToolName, setActiveToolName] = useState<string | null>(null);
  const throttledStreamingText = useThrottle(streamingText, 100);
  const scrollEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const { notifyConversationUpdate, setRender } = useVisualState();

  // Scroll to bottom on new messages (throttled during streaming)
  useEffect(() => {
    scrollEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, throttledStreamingText, activePlan]);

  // Load history on mount
  useEffect(() => {
    fetch("/api/chat/history")
      .then((res) => res.json())
      .then((data) => {
        if (data.messages?.length) {
          const loaded: ChatMessage[] = data.messages
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
          setMessages(loaded);
        }
      })
      .catch(() => {
        // Engine not available on mount
      });
  }, []);

  // Recover pending plan on page refresh
  useEffect(() => {
    fetch("/api/chat/plan/status")
      .then((res) => res.json())
      .then((data) => {
        if (data.active && data.plan) {
          setActivePlan({
            plan_id: data.plan.plan_id,
            plan_text: data.plan.plan_text,
            original_message: data.plan.original_message,
            status: data.plan.status,
          });
        }
      })
      .catch(() => {
        // Engine not available
      });
  }, []);

  // Keyboard shortcut: Ctrl/Cmd+Shift+P toggles plan mode
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === "P") {
        e.preventDefault();
        setPlanMode((prev) => !prev);
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const sendPlanMessage = useCallback(async (overrideText?: string) => {
    const text = overrideText || input.trim();
    if (!text || isStreaming || isPlanning) return;

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

    try {
      const res = await fetch("/api/chat/plan/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        let errorText = `Server error (${res.status}). Please try again.`;
        try {
          const errBody = await res.json();
          if (errBody.error) errorText = errBody.error;
        } catch { /* ignore */ }
        setMessages((prev) => [
          ...prev,
          { id: `err-${Date.now()}`, role: "assistant", content: errorText, timestamp: new Date() },
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

      const handleSSEEvent = (eventType: string, data: string) => {
        try {
          const parsed = JSON.parse(data);
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
            });
          } else if (eventType === "tool_start") {
            setActiveToolName(parsed.tool || null);
          } else if (eventType === "tool_end") {
            setActiveToolName(null);
          } else if (eventType === "done") {
            fullResponse = parsed.text || fullResponse;
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

      // If no plan event was received, show the response as a regular message
      if (!gotPlanEvent && fullResponse.trim()) {
        setMessages((prev) => [
          ...prev,
          {
            id: `asst-${Date.now()}`,
            role: "assistant",
            content: stripResidualMarkers(fullResponse).trim(),
            timestamp: new Date(),
          },
        ]);
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setMessages((prev) => [
          ...prev,
          { id: `err-${Date.now()}`, role: "assistant", content: "Something went wrong. Please try again.", timestamp: new Date() },
        ]);
      }
    } finally {
      setIsPlanning(false);
      setStreamingText("");
      setActiveToolName(null);
      abortRef.current = null;
    }
  }, [input, isStreaming, isPlanning]);

  const sendMessage = useCallback(async () => {
    if (planMode) {
      return sendPlanMessage();
    }

    const text = input.trim();
    if (!text || isStreaming) return;

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      content: text,
      timestamp: new Date(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setIsStreaming(true);
    setStreamingText("");

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const res = await fetch("/api/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        let errorText = `Server error (${res.status}). Please try again.`;
        try {
          const errBody = await res.json();
          if (errBody.error) errorText = errBody.error;
        } catch { /* ignore */ }
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`, role: "assistant",
          content: errorText, timestamp: new Date(),
        };
        setMessages((prev) => [...prev, errorMsg]);
        setIsStreaming(false);
        setStreamingText("");
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let sseEventType = "";
      let sseData = "";
      let fullResponse = "";
      const collectedAgentData: Record<string, unknown> = {};

      const handleSSEEvent = (eventType: string, data: string) => {
        try {
          const parsed = JSON.parse(data);
          if (eventType === "delta") {
            fullResponse += parsed.text || "";
            setStreamingText(fullResponse);
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
            });
          } else if (eventType === "done") {
            fullResponse = parsed.text || fullResponse;
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

      // Finalize the message
      const assistantMsg: ChatMessage = {
        id: `asst-${Date.now()}`,
        role: "assistant",
        content: stripResidualMarkers(fullResponse).trim(),
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, assistantMsg]);

      // Trigger dashboard update OUTSIDE the state updater (calling state setters
      // inside another setState updater can cause React to drop the batched updates)
      const recentMessages = [
        { role: userMsg.role, content: userMsg.content },
        { role: assistantMsg.role, content: assistantMsg.content },
      ].filter((m) => m.content.trim());

      notifyConversationUpdate(
        recentMessages,
        Object.keys(collectedAgentData).length > 0 ? collectedAgentData : undefined
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`,
          role: "assistant",
          content: "Something went wrong. Please try again.",
          timestamp: new Date(),
        };
        setMessages((prev) => [...prev, errorMsg]);
      }
    } finally {
      setIsStreaming(false);
      setStreamingText("");
      abortRef.current = null;
    }
  }, [input, isStreaming, planMode, sendPlanMessage, notifyConversationUpdate, setRender]);

  const handlePlanApprove = useCallback(async () => {
    if (!activePlan) return;
    setPlanMode(false);
    setIsPlanExecuting(true);
    setStreamingText("");

    try {
      const res = await fetch("/api/chat/plan/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: activePlan.plan_id }),
      });

      setActivePlan(null);

      if (!res.ok || !res.body) {
        setIsPlanExecuting(false);
        return;
      }

      // Stream the execution response
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let sseEventType = "";
      let sseData = "";
      let fullResponse = "";

      const processLine = (line: string) => {
        if (line === "") {
          if (sseData) {
            try {
              const parsed = JSON.parse(sseData);
              if (sseEventType === "delta") {
                fullResponse += parsed.text || "";
                setStreamingText(fullResponse);
              } else if (sseEventType === "done") {
                fullResponse = parsed.text || fullResponse;
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
          if (sseEventType === "done") fullResponse = parsed.text || fullResponse;
        } catch { /* skip */ }
      }

      setStreamingText("");
      if (fullResponse.trim()) {
        setMessages((prev) => [
          ...prev,
          {
            id: `asst-${Date.now()}`,
            role: "assistant",
            content: stripResidualMarkers(fullResponse).trim(),
            timestamp: new Date(),
          },
        ]);
      }
    } catch {
      // ignore
    } finally {
      setIsPlanExecuting(false);
      setStreamingText("");
    }
  }, [activePlan]);

  const handlePlanReject = useCallback(async () => {
    if (!activePlan) return;
    setPlanMode(false);
    try {
      await fetch("/api/chat/plan/reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan_id: activePlan.plan_id }),
      });
    } catch {
      // ignore
    }
    setActivePlan(null);
    setShowFeedbackInput(false);
    setPlanFeedback("");
  }, [activePlan]);

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

  const handleAbort = () => {
    abortRef.current?.abort();
  };

  const suggestedPrompts = [
    "How's everything running?",
    "Show my contacts",
    "Check the inbox",
    "What happened today?",
  ];

  return (
    <div className="h-full w-full flex flex-col bg-background" data-testid="chat-panel">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border flex items-center gap-2">
        <div className="relative">
          <div className="w-2 h-2 rounded-full bg-emerald-400" />
          <div className="absolute inset-0 w-2 h-2 rounded-full bg-emerald-400 animate-ping opacity-40" />
        </div>
        <span className="text-sm font-semibold">{process.env.NEXT_PUBLIC_AI_NAME || "Robothor"}</span>
        {planMode && (
          <Badge
            variant="outline"
            className="border-amber-500/50 text-amber-400 text-[10px] px-1.5 py-0"
            data-testid="plan-mode-badge"
          >
            Plan Mode
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
                    className="text-xs px-3 py-1.5 rounded-full border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
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
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {msg.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p>{msg.content}</p>
                )}
              </div>
            </div>
          ))}

          {/* Plan approval card */}
          {activePlan && !isPlanExecuting && (
            <div className="flex justify-start" data-testid="plan-card">
              <div className="max-w-[90%] rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 space-y-3">
                <div className="flex items-center gap-2 text-sm font-semibold text-amber-400">
                  <ClipboardList className="w-4 h-4" />
                  <span>Proposed Plan</span>
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
                    className="bg-emerald-600 hover:bg-emerald-700 text-white"
                    data-testid="plan-approve"
                  >
                    <Check className="w-3.5 h-3.5 mr-1" />
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setShowFeedbackInput((prev) => !prev)}
                    className="text-amber-400 hover:text-amber-300 hover:bg-amber-500/10"
                    data-testid="plan-edit"
                  >
                    <MessageSquareText className="w-3.5 h-3.5 mr-1" />
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={handlePlanReject}
                    className="text-red-400 hover:text-red-300 hover:bg-red-500/10"
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
                      className="w-full resize-none rounded-md border border-amber-500/30 bg-background px-3 py-2 text-sm min-h-[60px] focus:outline-none focus:ring-1 focus:ring-amber-500/50"
                      data-testid="plan-feedback-input"
                    />
                    <Button
                      size="sm"
                      onClick={handlePlanRevise}
                      disabled={!planFeedback.trim()}
                      className="bg-amber-600 hover:bg-amber-700 text-white"
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
              <div className={`max-w-[85%] rounded-lg px-3 py-2 text-sm bg-muted ${isPlanning ? "border border-amber-500/30" : ""}`}>
                {isPlanning ? (
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-amber-400" data-testid="planning-indicator">
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
                ) : throttledStreamingText ? (
                  <div className="prose prose-sm prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {throttledStreamingText}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5 py-1">
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                    <span className="typing-dot" />
                  </div>
                )}
              </div>
            </div>
          )}
          <div ref={scrollEndRef} />
        </div>
      </div>

      {/* Input area */}
      <div className="p-3 border-t border-border">
        <div className="flex items-end gap-2">
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => {
                    setPlanMode((prev) => !prev);
                    inputRef.current?.focus();
                  }}
                  className={planMode ? "text-amber-400 bg-amber-500/10 hover:bg-amber-500/20" : "text-muted-foreground hover:text-foreground"}
                  disabled={isStreaming || isPlanExecuting || isPlanning}
                  data-testid="plan-toggle"
                >
                  <ClipboardList className="w-4 h-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top">
                <p>Plan mode ({navigator?.platform?.includes("Mac") ? "⌘" : "Ctrl"}+Shift+P)</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={planMode ? "Describe what you want planned..." : "Ask me anything..."}
            className={`flex-1 resize-none rounded-lg border bg-background px-3 py-2 text-sm min-h-[40px] max-h-[120px] focus:outline-none focus:ring-1 ${planMode ? "border-amber-500/30 focus:ring-amber-500/50" : "border-border focus:ring-ring"}`}
            rows={1}
            disabled={isStreaming || isPlanExecuting || isPlanning}
            data-testid="chat-input"
          />
          {isStreaming || isPlanning ? (
            <Button
              size="icon"
              variant="ghost"
              onClick={handleAbort}
              data-testid="abort-button"
            >
              <Square className="w-4 h-4" />
            </Button>
          ) : (
            <Button
              size="icon"
              onClick={sendMessage}
              disabled={!input.trim() || isPlanExecuting}
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
