import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RecoveryStop } from "../recovery-stop";

const request = { requestId: "original-id", agent: "original-agent" };

describe("stop during recovery", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("requires durable acknowledgement and retries only the stop command", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true, durable_stopped: false }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true, durable_stopped: true }) });
    vi.stubGlobal("fetch", fetch);
    render(<RecoveryStop request={request} recorded={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop this request" }));
    await screen.findByText(/Stop has not been confirmed/);
    fireEvent.click(screen.getByRole("button", { name: "Stop this request" }));
    await screen.findByText("Stop recorded.");
    expect(fetch).toHaveBeenCalledTimes(2);
    for (const [url, options] of fetch.mock.calls) {
      expect(url).toBe("/api/chat/abort");
      expect(options.method).toBe("POST");
      expect(JSON.parse(options.body)).toEqual({ request_id: "original-id", agent: "original-agent" });
    }
  });

  it("does not send duplicate stops while awaiting acknowledgement and aborts on unmount", async () => {
    const fetch = vi.fn().mockImplementation(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetch);
    const { unmount } = render(<RecoveryStop request={request} recorded={false} />);
    const button = screen.getByRole("button", { name: "Stop this request" });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const signal = fetch.mock.calls[0][1].signal;
    unmount();
    expect(signal.aborted).toBe(true);
  });
});
