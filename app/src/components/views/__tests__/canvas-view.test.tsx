import { render, screen, act, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { CanvasView } from "../canvas-view";

afterEach(() => vi.restoreAllMocks());

describe("CanvasView", () => {
  it("renders the canvas tab with the sandboxed renderer", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div>hi</div>" }) } as Response);
    render(<CanvasView visible />);
    expect(await screen.findByTestId("canvas-view")).toBeTruthy();
    // the sandboxed iframe is present and never same-origin
    const iframe = document.querySelector('[data-testid="canvas-srcdoc-renderer"]') as HTMLIFrameElement;
    expect(iframe).toBeTruthy();
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts");
    expect(iframe.getAttribute("sandbox") ?? "").not.toMatch(/allow-same-origin/);
  });

  it("loads the dashboard HTML via POST with no body", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div>hi</div>" }) } as Response);
    render(<CanvasView visible />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith("/api/dashboard/welcome", expect.objectContaining({ method: "POST" }));
  });

  it("shows a confirm dialog in parent chrome built from the real action, not the iframe label", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div></div>" }) } as Response);
    render(<CanvasView visible />);
    const iframe = (await screen.findByTestId("canvas-srcdoc-renderer")) as HTMLIFrameElement;
    // simulate a hostile propose arriving from the iframe
    act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        origin: "null", source: iframe.contentWindow,
        data: { __robothor: true, kind: "propose", reqId: "p1", action: "set_flag",
                args: { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" }, label: "Refresh" },
      }));
    });
    const dialog = await screen.findByTestId("canvas-confirm");
    expect(dialog.textContent).toContain("ROBOTHOR_INJECTION_SCAN_MODE");
    expect(dialog.textContent).toContain("off");
    expect(dialog.textContent).not.toMatch(/refresh/i);
    expect(screen.getByTestId("canvas-confirm-accept")).toBeTruthy();
    expect(screen.getByTestId("canvas-confirm-cancel")).toBeTruthy();
  });

  it("disables the Confirm button after the first click so a fast double-click cannot double-submit", async () => {
    const fetchMock = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div></div>" }) } as Response);
    render(<CanvasView visible />);
    const iframe = (await screen.findByTestId("canvas-srcdoc-renderer")) as HTMLIFrameElement;
    act(() => {
      window.dispatchEvent(new MessageEvent("message", {
        origin: "null", source: iframe.contentWindow,
        data: { __robothor: true, kind: "propose", reqId: "p1", action: "set_flag",
                args: { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" }, label: "Refresh" },
      }));
    });
    const acceptButton = await screen.findByTestId("canvas-confirm-accept");
    expect(acceptButton).not.toBeDisabled();

    fireEvent.click(acceptButton);
    expect(acceptButton).toBeDisabled();

    const patchCalls = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "PATCH");
    await waitFor(() => expect(patchCalls.length).toBeGreaterThan(0));
    expect(patchCalls.length).toBe(1);
  });

  it("injects the shim + binder and presents the canvas as live", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({ html: "<div data-read='get_fleet'></div>" }) } as Response);
    const { container } = render(<CanvasView visible />);
    const iframe = await screen.findByTestId("canvas-srcdoc-renderer");
    const srcdoc = iframe.getAttribute("srcdoc") ?? "";
    expect(srcdoc).toMatch(/self\.robothor/); // shim present
    expect(srcdoc).toMatch(/data-read/); // binder present (scans data-read)
    expect(container.textContent).not.toMatch(/gated/i); // caption updated
  });
});
