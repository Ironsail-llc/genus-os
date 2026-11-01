import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { ChatPanel } from "../chat-panel";

vi.mock("@/hooks/use-visual-state", () => ({
  useVisualState: () => ({ notifyConversationUpdate: vi.fn(), setRender: vi.fn() }),
}));
vi.mock("@/hooks/use-throttle", () => ({ useThrottle: (value: string) => value }));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function doneStream() {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(`event: done\ndata: ${JSON.stringify({ text: "ok" })}\n\n`));
      controller.close();
    },
  });
}

/** Records every /api/chat/send body; history answers with `history`. */
function engine(history: unknown[] = []) {
  const sends: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url) === "/api/chat/send") {
      sends.push(JSON.parse(String(init?.body ?? "{}")));
      return new Response(doneStream(), { headers: { "content-type": "text/event-stream" } });
    }
    if (String(url).startsWith("/api/chat/history")) {
      return { ok: true, json: async () => ({ messages: history }) };
    }
    return { ok: true, json: async () => ({ messages: [], agents: [] }) };
  });
  vi.stubGlobal("fetch", fetchMock);
  return sends;
}

const file = (name: string, type: string, body: BlobPart = "hello") =>
  new File([body], name, { type });

function attach(...files: File[]) {
  fireEvent.change(screen.getByTestId("attach-input"), { target: { files } });
}

describe("attaching files to a chat turn", () => {
  it("has a labelled attach button that opens a hidden multi-file input", () => {
    engine();
    render(<ChatPanel />);
    const button = screen.getByTestId("attach-button");
    expect(button).toHaveAttribute("aria-label", "Attach files");
    const input = screen.getByTestId("attach-input") as HTMLInputElement;
    expect(input.type).toBe("file");
    expect(input.multiple).toBe(true);
    expect(input).not.toBeVisible();
    const click = vi.spyOn(input, "click");
    fireEvent.click(button);
    expect(click).toHaveBeenCalled();
  });

  it("shows a chip with each file's name and size, and the limits", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("notes.md", "text/markdown", "x".repeat(2048)), file("shot.png", "image/png"));
    const chips = await screen.findAllByTestId("attachment-chip");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("notes.md");
    expect(chips[0]).toHaveTextContent("2.0 KB");
    expect(chips[1]).toHaveTextContent("shot.png");
    expect(chips[1]).toHaveTextContent("5 B");
    const limits = screen.getByTestId("attach-limits");
    expect(limits).toHaveTextContent(/PNG, JPG, GIF, WebP/);
    expect(limits).toHaveTextContent(/5 MB each/);
    expect(limits).toHaveTextContent(/3 per message/);
  });

  it("removes a chip with its remove control", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("notes.md", "text/markdown"), file("data.csv", "text/csv"));
    await screen.findAllByTestId("attachment-chip");
    fireEvent.click(screen.getByRole("button", { name: "Remove notes.md" }));
    await waitFor(() => expect(screen.getAllByTestId("attachment-chip")).toHaveLength(1));
    expect(screen.getByTestId("attachment-chip")).toHaveTextContent("data.csv");
  });

  it("refuses a file over 5 MB inline, never with a browser alert", async () => {
    engine();
    const alert = vi.fn();
    vi.stubGlobal("alert", alert);
    render(<ChatPanel />);
    attach(file("big.txt", "text/plain", new Uint8Array(5 * 1024 * 1024 + 1)));
    expect(await screen.findByTestId("attach-refusal")).toHaveTextContent("big.txt is larger than 5 MB");
    expect(screen.queryByTestId("attachment-chip")).toBeNull();
    expect(alert).not.toHaveBeenCalled();
  });

  it("refuses an unsupported type inline", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("archive.zip", "application/zip"));
    expect(await screen.findByTestId("attach-refusal")).toHaveTextContent("archive.zip is not a supported type");
    expect(screen.queryByTestId("attachment-chip")).toBeNull();
  });

  it("refuses a fourth file and keeps the first three", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("1.txt", "text/plain"), file("2.txt", "text/plain"), file("3.txt", "text/plain"), file("4.txt", "text/plain"));
    expect(await screen.findByTestId("attach-refusal")).toHaveTextContent("Only 3 files per message: 4.txt was not added");
    expect(screen.getAllByTestId("attachment-chip")).toHaveLength(3);
  });

  it("refuses a file whose extension is not supported, whatever type it claims", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("tool.exe", "text/plain"), file("evil.png", "text/plain"));
    const refusal = await screen.findByTestId("attach-refusal");
    expect(refusal).toHaveTextContent("tool.exe is not a supported type");
    expect(refusal).toHaveTextContent("evil.png is not a supported type");
    expect(screen.queryByTestId("attachment-chip")).toBeNull();
  });

  it("accepts a markdown file the browser gave no type, by its extension", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("README.md", ""));
    expect(await screen.findByTestId("attachment-chip")).toHaveTextContent("README.md");
  });

  it("adds pasted and dropped files", async () => {
    engine();
    render(<ChatPanel />);
    fireEvent.paste(screen.getByTestId("chat-input"), { clipboardData: { files: [file("pasted.png", "image/png")] } });
    expect(await screen.findByTestId("attachment-chip")).toHaveTextContent("pasted.png");
    fireEvent.drop(screen.getByTestId("chat-composer"), { dataTransfer: { files: [file("dropped.json", "application/json")] } });
    await waitFor(() => expect(screen.getAllByTestId("attachment-chip")).toHaveLength(2));
    expect(screen.getAllByTestId("attachment-chip")[1]).toHaveTextContent("dropped.json");
  });

  it("sends {name, mime, size, data} in base64 with the message, then clears the chips", async () => {
    const sends = engine();
    render(<ChatPanel />);
    attach(file("notes.md", "text/markdown", "hello"));
    await screen.findByTestId("attachment-chip");
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "read this" } });
    fireEvent.click(screen.getByTestId("send-button"));
    await waitFor(() => expect(sends).toHaveLength(1));
    expect(sends[0].message).toBe("read this");
    expect(sends[0].attachments).toEqual([
      { name: "notes.md", mime: "text/markdown", size: 5, data: btoa("hello") },
    ]);
    await waitFor(() => expect(screen.queryByTestId("attachment-chip")).toBeNull());
    const bubble = screen.getAllByTestId("message-user").at(-1)!;
    expect(within(bubble).getByTestId("message-attachment")).toHaveTextContent("notes.md");
  });

  it("can send files with no text", async () => {
    const sends = engine();
    render(<ChatPanel />);
    attach(file("shot.png", "image/png"));
    await screen.findByTestId("attachment-chip");
    expect(screen.getByTestId("send-button")).toBeEnabled();
    fireEvent.click(screen.getByTestId("send-button"));
    await waitFor(() => expect(sends).toHaveLength(1));
    expect(sends[0].message).toBe("");
    expect((sends[0].attachments as unknown[]).length).toBe(1);
  });

  it("a plain message carries no attachments field", async () => {
    const sends = engine();
    render(<ChatPanel />);
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "hi" } });
    fireEvent.click(screen.getByTestId("send-button"));
    await waitFor(() => expect(sends).toHaveLength(1));
    expect("attachments" in sends[0]).toBe(false);
  });

  it("history shows the attachment names from extras.attachments, without the note", async () => {
    engine([
      {
        role: "user",
        content: "what is this?\n\n[1 file attached — kept on disk; use the paths below]\n- shot.png — image, 5 B\n   path: /inbox/shot.png",
        extras: { attachments: [{ name: "shot.png", size: 5, kind: "image", path: "/inbox/shot.png" }] },
      },
      { role: "assistant", content: "A screenshot." },
    ]);
    render(<ChatPanel />);
    const chip = await screen.findByTestId("message-attachment");
    expect(chip).toHaveTextContent("shot.png");
    const bubble = screen.getByTestId("message-user");
    expect(bubble).toHaveTextContent("what is this?");
    expect(bubble).not.toHaveTextContent("kept on disk");
  });

  it("history also reads rows restored from the store, where they sit at the top level", async () => {
    engine([
      { role: "user", content: "see", attachments: [{ name: "a.txt", size: 2, kind: "document" }] },
    ]);
    render(<ChatPanel />);
    expect(await screen.findByTestId("message-attachment")).toHaveTextContent("a.txt");
  });

  it("a paste that carries text pastes the text and ignores the image beside it", async () => {
    engine();
    render(<ChatPanel />);
    const input = screen.getByTestId("chat-input");
    const notPrevented = fireEvent.paste(input, {
      clipboardData: { types: ["text/plain", "text/html", "Files"], files: [file("cells.png", "image/png")], getData: () => "A1\tB1" },
    });
    expect(notPrevented).toBe(true);
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId("attachment-chip")).toBeNull();
    expect(screen.queryByTestId("attach-refusal")).toBeNull();
  });

  it("a paste with only files attaches them", async () => {
    engine();
    render(<ChatPanel />);
    const notPrevented = fireEvent.paste(screen.getByTestId("chat-input"), {
      clipboardData: { types: ["Files"], files: [file("shot.png", "image/png")] },
    });
    expect(notPrevented).toBe(false);
    expect(await screen.findByTestId("attachment-chip")).toHaveTextContent("shot.png");
  });

  it("refuses files pasted or dropped in Plan mode, inline", async () => {
    engine();
    render(<ChatPanel />);
    fireEvent.click(screen.getByTestId("plan-toggle"));
    fireEvent.paste(screen.getByTestId("chat-input"), { clipboardData: { files: [file("pasted.png", "image/png")] } });
    expect(await screen.findByTestId("attach-refusal")).toHaveTextContent("Files go with an ordinary message, not in Plan or Deep mode");
    fireEvent.drop(screen.getByTestId("chat-composer"), { dataTransfer: { files: [file("dropped.txt", "text/plain")] } });
    await waitFor(() => expect(screen.getByTestId("attach-refusal")).toBeVisible());
    expect(screen.queryByTestId("attachment-chip")).toBeNull();
  });

  it("removes pending files, with a note, when Plan mode is switched on", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("notes.md", "text/markdown"));
    await screen.findByTestId("attachment-chip");
    fireEvent.click(screen.getByTestId("plan-toggle"));
    await waitFor(() => expect(screen.queryByTestId("attachment-chip")).toBeNull());
    expect(screen.getByTestId("attach-refusal")).toHaveTextContent("notes.md was removed");
    expect(screen.getByTestId("send-button")).toBeDisabled();
  });

  it("removes pending files when Deep mode is switched on", async () => {
    engine();
    render(<ChatPanel />);
    attach(file("notes.md", "text/markdown"));
    await screen.findByTestId("attachment-chip");
    fireEvent.click(screen.getByTestId("deep-toggle"));
    await waitFor(() => expect(screen.queryByTestId("attachment-chip")).toBeNull());
    expect(screen.getByTestId("send-button")).toBeDisabled();
  });
});
