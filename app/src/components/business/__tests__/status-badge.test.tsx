import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { StatusBadge } from "../status-badge";

describe("StatusBadge", () => {
  it("renders a text label for each status, never color alone", () => {
    const { rerender } = render(<StatusBadge status="ok" />);
    expect(screen.getByTestId("status-badge").textContent).toMatch(/healthy/i);

    rerender(<StatusBadge status="running" />);
    expect(screen.getByTestId("status-badge").textContent).toMatch(/running/i);

    rerender(<StatusBadge status="degraded" />);
    expect(screen.getByTestId("status-badge").textContent).toMatch(/degraded/i);

    rerender(<StatusBadge status="failing" />);
    expect(screen.getByTestId("status-badge").textContent).toMatch(/failing/i);

    rerender(<StatusBadge status="idle" />);
    expect(screen.getByTestId("status-badge").textContent).toMatch(/idle/i);
  });

  it("accepts a custom label overriding the default", () => {
    render(<StatusBadge status="ok" label="All systems normal" />);
    expect(screen.getByTestId("status-badge").textContent).toBe("All systems normal");
  });

  it("normalizes unknown engine status strings to idle", () => {
    render(<StatusBadge status={"???" as never} />);
    expect(screen.getByTestId("status-badge").textContent).toMatch(/idle/i);
  });

  it("maps common engine result strings via fromEngineStatus", async () => {
    const { fromEngineStatus } = await import("../status-badge");
    expect(fromEngineStatus("completed")).toBe("ok");
    expect(fromEngineStatus("success")).toBe("ok");
    expect(fromEngineStatus("running")).toBe("running");
    expect(fromEngineStatus("in_progress")).toBe("running");
    expect(fromEngineStatus("degraded")).toBe("degraded");
    expect(fromEngineStatus("timeout")).toBe("degraded");
    expect(fromEngineStatus("failed")).toBe("failing");
    expect(fromEngineStatus("error")).toBe("failing");
    expect(fromEngineStatus(null)).toBe("idle");
    expect(fromEngineStatus(undefined)).toBe("idle");
  });
});
