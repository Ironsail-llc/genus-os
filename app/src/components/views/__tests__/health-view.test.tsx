import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { HealthView } from "../health-view";

afterEach(() => vi.restoreAllMocks());

describe("HealthView", () => {
  it("renders sections; a failed unit and a stale backup are amber, never green", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue({
      ok: true, json: async () => ({
        wal: { archived_count: 10, failed_count: 0, last_archived_time: "x", status: "ok" },
        backups: [{ unit: "robothor-backup-local.timer", age_hours: 40, status: "warn" }],
        failed_units: ["robothor-foo.service"],
        disks: [{ mount: "/", used_pct: 40, free_gb: 100, status: "ok" }],
        generated_at: "x",
      }),
    } as Response);
    render(<HealthView visible />);
    const units = await screen.findByTestId("health-units");
    expect(units.className).toMatch(/warning/i);        // a failed unit warns
    const backups = screen.getByTestId("health-backups");
    expect(backups.className).toMatch(/warning/i);      // stale backup warns
    expect(backups.className).not.toMatch(/success|emerald|green/i);
    const wal = screen.getByTestId("health-wal");
    expect(wal.className).toMatch(/success/i);          // ok is affirmative
  });

  it("does not fetch when hidden", () => {
    const spy = vi.spyOn(global, "fetch").mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    render(<HealthView visible={false} />);
    expect(spy).not.toHaveBeenCalled();
  });
});
