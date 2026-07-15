import { describe, it, expect } from "vitest";
import { resolveReadOp, resolveProposeAction, isCanvasMessage } from "../canvas-bridge";

describe("resolveReadOp", () => {
  it("maps known ops to bridge paths", () => {
    expect(resolveReadOp("get_flags")).toEqual({ path: "/api/controls" });
    expect(resolveReadOp("get_fleet")).toEqual({ path: "/api/fleet" });
    expect(resolveReadOp("get_health")).toEqual({ path: "/api/health/system" });
  });
  it("validates and injects id args as path segments", () => {
    expect(resolveReadOp("get_agent", { id: "main" })).toEqual({ path: "/api/fleet/main" });
    expect(resolveReadOp("get_run", { id: "abc-123" })).toEqual({ path: "/api/runs/abc-123" });
    expect(resolveReadOp("get_workflow_runs", { id: "intel" })).toEqual({ path: "/api/workflows/intel/runs" });
  });
  it("drops unknown ops", () => {
    expect(resolveReadOp("delete_everything")).toBeNull();
    expect(resolveReadOp("get_secret")).toBeNull();
  });
  it("rejects id args that could escape the path (injection)", () => {
    expect(resolveReadOp("get_agent", { id: "../controls" })).toBeNull();
    expect(resolveReadOp("get_agent", { id: "a/b" })).toBeNull();
    expect(resolveReadOp("get_agent", { id: "" })).toBeNull();
    expect(resolveReadOp("get_agent", {})).toBeNull();           // id required
    expect(resolveReadOp("get_agent", { id: 123 as unknown as string })).toBeNull();
  });
});

describe("resolveProposeAction", () => {
  it("resolves the only proposable action, set_flag, from PARENT-derived data", () => {
    const r = resolveProposeAction("set_flag", { name: "ROBOTHOR_INJECTION_SCAN_MODE", value: "off" });
    expect(r).not.toBeNull();
    expect(r!.method).toBe("PATCH");
    expect(r!.path).toBe("/api/controls/ROBOTHOR_INJECTION_SCAN_MODE");
    expect(r!.body).toMatchObject({ value: "off" });
    // describe is built here, from the action — never from an iframe label
    expect(r!.describe).toContain("ROBOTHOR_INJECTION_SCAN_MODE");
    expect(r!.describe).toContain("off");
  });
  it("drops any non-whitelisted action", () => {
    expect(resolveProposeAction("delete_flag", { name: "x" })).toBeNull();
    expect(resolveProposeAction("exec", { cmd: "rm -rf /" })).toBeNull();
    expect(resolveProposeAction("trigger_agent", { id: "main" })).toBeNull();  // out of scope in P3
  });
  it("rejects a set_flag with a malformed flag name (path injection / unknown flag shape)", () => {
    expect(resolveProposeAction("set_flag", { name: "../../etc", value: "off" })).toBeNull();
    expect(resolveProposeAction("set_flag", { name: "not a flag", value: "off" })).toBeNull();
    expect(resolveProposeAction("set_flag", { name: "ROBOTHOR_X", value: 5 as unknown as string })).toBeNull();
  });
});

describe("isCanvasMessage", () => {
  it("accepts tagged read/propose messages and rejects everything else", () => {
    expect(isCanvasMessage({ __robothor: true, kind: "read", reqId: "1", op: "get_fleet" })).toBe(true);
    expect(isCanvasMessage({ __robothor: true, kind: "propose", action: "set_flag", args: {}, reqId: "2" })).toBe(true);
    expect(isCanvasMessage({ type: "srcdoc-height", height: 400 })).toBe(false);  // reserved renderer msg
    expect(isCanvasMessage({ type: "robothor:error" })).toBe(false);
    expect(isCanvasMessage({ kind: "read" })).toBe(false);                        // missing __robothor tag
    expect(isCanvasMessage("nope")).toBe(false);
  });
});
