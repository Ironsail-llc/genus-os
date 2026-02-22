/**
 * Tests for visual state action management (Phase 4).
 * Validates: submitAction, resolveAction, pendingAction, lastActionResult.
 */

import { describe, it, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import React from "react";
import { VisualStateProvider, useVisualState } from "@/hooks/use-visual-state";

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(VisualStateProvider, null, children);
}

describe("useVisualState — action management", () => {
  it("starts with no pending action or result", () => {
    const { result } = renderHook(() => useVisualState(), { wrapper });
    expect(result.current.pendingAction).toBeNull();
    expect(result.current.lastActionResult).toBeNull();
  });

  it("submitAction sets pendingAction and clears lastActionResult", () => {
    const { result } = renderHook(() => useVisualState(), { wrapper });

    act(() => {
      result.current.submitAction({
        tool: "crm_health",
        params: {},
        id: "action-1",
      });
    });

    expect(result.current.pendingAction).toEqual({
      tool: "crm_health",
      params: {},
      id: "action-1",
    });
    expect(result.current.lastActionResult).toBeNull();
  });

  it("resolveAction sets lastActionResult and clears pendingAction", () => {
    const { result } = renderHook(() => useVisualState(), { wrapper });

    // Submit
    act(() => {
      result.current.submitAction({
        tool: "list_people",
        params: { limit: 5 },
        id: "action-2",
      });
    });

    // Resolve
    act(() => {
      result.current.resolveAction({
        id: "action-2",
        success: true,
        data: { people: [] },
      });
    });

    expect(result.current.pendingAction).toBeNull();
    expect(result.current.lastActionResult).toEqual({
      id: "action-2",
      success: true,
      data: { people: [] },
    });
  });

  it("resolveAction with error sets error in result", () => {
    const { result } = renderHook(() => useVisualState(), { wrapper });

    act(() => {
      result.current.submitAction({
        tool: "create_note",
        params: { title: "Test" },
        id: "action-3",
      });
    });

    act(() => {
      result.current.resolveAction({
        id: "action-3",
        success: false,
        error: "Permission denied",
      });
    });

    expect(result.current.pendingAction).toBeNull();
    expect(result.current.lastActionResult?.success).toBe(false);
    expect(result.current.lastActionResult?.error).toBe("Permission denied");
  });

  it("new submitAction clears previous result", () => {
    const { result } = renderHook(() => useVisualState(), { wrapper });

    // First action cycle
    act(() => {
      result.current.submitAction({ tool: "crm_health", params: {}, id: "a1" });
    });
    act(() => {
      result.current.resolveAction({ id: "a1", success: true, data: "ok" });
    });
    expect(result.current.lastActionResult).not.toBeNull();

    // Second submit clears previous result
    act(() => {
      result.current.submitAction({ tool: "list_people", params: {}, id: "a2" });
    });
    expect(result.current.lastActionResult).toBeNull();
    expect(result.current.pendingAction?.id).toBe("a2");
  });
});
