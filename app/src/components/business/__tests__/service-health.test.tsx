import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ServiceHealth } from "../service-health";
import type { ServiceHealth as ServiceHealthType } from "@/lib/api/types";

/**
 * The service cards speak the operator's language: a human label per service,
 * four distinct states (healthy / degraded / unhealthy / disabled), and the
 * reason next to the state. "Off" is calm, not red.
 */
const services: ServiceHealthType[] = [
  { name: "bridge", label: "API bridge", url: "u", status: "healthy", responseTime: 12, detail: "checks: crm ok, memory ok" },
  { name: "engine", label: "Agent engine", url: "u", status: "degraded", responseTime: 30, detail: "redis error:timeout" },
  { name: "vision", label: "Vision (camera)", url: "u", status: "disabled", responseTime: 4, detail: "switched off (mode: disabled)" },
  { name: "orchestrator", label: "Memory & retrieval", url: "u", status: "unhealthy", responseTime: 5000, detail: "unreachable" },
];

describe("ServiceHealth", () => {
  it("labels each service for a human and exposes all four states with their reasons", () => {
    render(<ServiceHealth services={services} overallStatus="degraded" />);
    expect(screen.getByText("API bridge")).toBeInTheDocument();
    expect(screen.getByText("Agent engine")).toBeInTheDocument();
    expect(screen.getByText("Vision (camera)")).toBeInTheDocument();
    expect(screen.getByText("Memory & retrieval")).toBeInTheDocument();

    const cards = screen.getAllByTestId("service-card");
    const byName = Object.fromEntries(cards.map((c) => [c.getAttribute("data-service"), c]));
    expect(byName.bridge.getAttribute("data-status")).toBe("healthy");
    expect(byName.engine.getAttribute("data-status")).toBe("degraded");
    expect(byName.vision.getAttribute("data-status")).toBe("disabled");
    expect(byName.orchestrator.getAttribute("data-status")).toBe("unhealthy");

    expect(byName.vision.textContent).toContain("Off");
    expect(byName.vision.textContent).toContain("switched off");
    expect(byName.engine.textContent).toContain("redis error:timeout");
    expect(byName.orchestrator.textContent).toContain("unreachable");
    expect(screen.getByTestId("overall-status").textContent).toBe("Degraded");
  });

  it("says all systems operational when the overall status is ok, even with a service switched off", () => {
    render(
      <ServiceHealth
        services={[services[0], services[2]]}
        overallStatus="ok"
      />,
    );
    expect(screen.getByTestId("overall-status").textContent).toBe("All systems operational");
  });
});
