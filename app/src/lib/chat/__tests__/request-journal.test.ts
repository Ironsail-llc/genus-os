import { beforeEach, describe, expect, it, vi } from "vitest";
import { forgetRequest, pendingRequests, rememberRequest } from "../request-journal";

const scope = "10000000-0000-4000-8000-000000000001";
const other = "10000000-0000-4000-8000-000000000002";
const request = "20000000-0000-4000-8000-000000000001";

describe("scoped pending request journal", () => {
  beforeEach(() => { sessionStorage.clear(); vi.restoreAllMocks(); });
  it("retains one identifier across readers and isolates conversations", () => {
    expect(rememberRequest(scope, request)).toBe(true);
    rememberRequest(scope, request);
    expect(pendingRequests(scope)).toEqual([request]);
    expect(pendingRequests(other)).toEqual([]);
    forgetRequest(other, request);
    expect(pendingRequests(scope)).toEqual([request]);
    forgetRequest(scope, request);
    expect(pendingRequests(scope)).toEqual([]);
    expect(sessionStorage.length).toBe(0);
  });
  it("ignores malformed stored data and never stores message content", () => {
    expect(rememberRequest(scope, "private message")).toBe(false);
    expect(rememberRequest("invalid", request)).toBe(false);
    sessionStorage.setItem("helm.chat.pending.v1." + scope, JSON.stringify([request, "private message", null]));
    expect(pendingRequests(scope)).toEqual([request]);
    sessionStorage.setItem("helm.chat.pending.v1." + scope, "broken JSON");
    expect(pendingRequests(scope)).toEqual([]);
  });
  it("does not break chat when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(rememberRequest(scope, request)).toBe(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(pendingRequests(scope)).toEqual([]);
    expect(() => forgetRequest(scope, request)).not.toThrow();
  });
});
