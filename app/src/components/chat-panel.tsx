"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/ui/button";
import { useVisualState } from "@/hooks/use-visual-state";
import { Send, Square, Loader2 } from "lucide-react";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

/** Strip any residual markers from history messages (loaded from gateway) */
function stripResidualMarkers(text: string): string {
  return text
    .replace(/\[DASHBOARD:\{[^]*?\}\]/g, "")
    .replace(/\[RENDER:[a-z_]+:[^]*?\]/g, "")
    .trim();
}

export function ChatPanel() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const scrollEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const { notifyConversationUpdate, setRender } = useVisualState();

  // Scroll to bottom on new messages
  useEffect(() => {
    scrollEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, streamingText]);

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
        // Gateway not available, that's ok
      });
  }, []);

  const sendMessage = useCallback(async () => {
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
        throw new Error("Failed to send message");
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let fullResponse = "";
      // Dashboard markers collected but not used — triage agent decides independently

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        sseBuffer += decoder.decode(value, { stream: true });

        // Parse SSE events
        const lines = sseBuffer.split("\n");
        sseBuffer = lines.pop() || "";

        for (let i = 0; i < lines.length; i++) {
          const line = lines[i];
          if (line.startsWith("data: ")) {
            const eventType =
              i > 0 && lines[i - 1].startsWith("event: ")
                ? lines[i - 1].slice(7)
                : "delta";
            const data = line.slice(6);
            try {
              const parsed = JSON.parse(data);

              if (eventType === "delta") {
                // Clean text — no markers, safe to display directly
                fullResponse += parsed.text || "";
                setStreamingText(fullResponse);
              } else if (eventType === "dashboard") {
                // Dashboard markers are informational — triage agent decides independently
              } else if (eventType === "render") {
                // Render markers still trigger immediately
                setRender({
                  component: parsed.component,
                  props: parsed.props,
                });
              } else if (eventType === "done") {
                fullResponse = parsed.text || fullResponse;
              }
            } catch {
              // Invalid JSON chunk, skip
            }
          }
        }
      }

      // Clear streaming state BEFORE adding the message to avoid duplicate display
      setIsStreaming(false);
      setStreamingText("");

      // Finalize the message
      const assistantMsg: ChatMessage = {
        id: `asst-${Date.now()}`,
        role: "assistant",
        content: fullResponse.trim(),
        timestamp: new Date(),
      };
      setMessages((prev) => {
        const updated = [...prev, assistantMsg];

        // Build recent messages for dashboard (last 3)
        const recentMessages = updated
          .filter((m) => m.content.trim())
          .slice(-3)
          .map((m) => ({ role: m.role, content: m.content }));

        // Auto-trigger conversation dashboard update (background, with triage)
        notifyConversationUpdate(recentMessages);

        return updated;
      });
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        const errorMsg: ChatMessage = {
          id: `err-${Date.now()}`,
          role: "assistant",
          content: "Sorry, I couldn't connect to the gateway. Please try again.",
          timestamp: new Date(),
        };
        setMessages((prev) => [...prev, errorMsg]);
      }
    } finally {
      setIsStreaming(false);
      setStreamingText("");
      abortRef.current = null;
    }
  }, [input, isStreaming, notifyConversationUpdate, setRender]);

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
        <div className="w-2 h-2 rounded-full bg-green-500" />
        <span className="text-sm font-semibold">Robothor</span>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4">
        <div className="space-y-4" data-testid="message-list">
          {messages.length === 0 && !isStreaming && (
            <div className="space-y-4" data-testid="empty-state">
              <p className="text-sm text-muted-foreground">
                Hey Philip. What can I help you with?
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
                    : "bg-muted"
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

          {/* Streaming indicator */}
          {isStreaming && (
            <div className="flex justify-start" data-testid="streaming-message">
              <div className="max-w-[85%] rounded-lg px-3 py-2 text-sm bg-muted">
                {streamingText ? (
                  <div className="prose prose-sm prose-invert max-w-none">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {streamingText}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-muted-foreground">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    <span>Thinking...</span>
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
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask me anything..."
            className="flex-1 resize-none rounded-lg border border-border bg-background px-3 py-2 text-sm min-h-[40px] max-h-[120px] focus:outline-none focus:ring-1 focus:ring-ring"
            rows={1}
            disabled={isStreaming}
            data-testid="chat-input"
          />
          {isStreaming ? (
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
              disabled={!input.trim()}
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
