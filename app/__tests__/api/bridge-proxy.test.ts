import { describe, it, expect, vi, beforeEach } from "vitest";

const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

import { GET, POST, DELETE } from "@/app/api/bridge/[...path]/route";
import { NextRequest } from "next/server";

function makeRequest(method: string, path: string, body?: string) {
  const url = `http://localhost:3004/api/bridge/${path}`;
  return new NextRequest(url, {
    method,
    body,
    headers: body ? { "Content-Type": "application/json" } : {},
  });
}

function makeContext(pathSegments: string[]) {
  return { params: Promise.resolve({ path: pathSegments }) };
}

describe("Bridge Proxy", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("GET /api/bridge/health proxies to localhost:9100/health", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ status: "ok" }),
    });

    const req = makeRequest("GET", "health");
    const res = await GET(req, makeContext(["health"]));
    const body = await res.json();

    expect(mockFetch).toHaveBeenCalledWith(
      "http://127.0.0.1:9100/health",
      expect.objectContaining({ method: "GET" })
    );
    expect(body).toEqual({ status: "ok" });
  });

  it("GET proxies query params", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve([]),
    });

    const req = new NextRequest(
      "http://localhost:3004/api/bridge/api/people?search=john"
    );
    await GET(req, makeContext(["api", "people"]));

    expect(mockFetch).toHaveBeenCalledWith(
      expect.stringContaining("http://127.0.0.1:9100/api/people?search=john"),
      expect.any(Object)
    );
  });

  it("POST proxies request body", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 201,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ id: "123" }),
    });

    const body = JSON.stringify({ title: "Test note", body: "Content" });
    const req = makeRequest("POST", "api/notes", body);
    const res = await POST(req, makeContext(["api", "notes"]));

    expect(res.status).toBe(201);
    expect(mockFetch).toHaveBeenCalledWith(
      "http://127.0.0.1:9100/api/notes",
      expect.objectContaining({ method: "POST" })
    );
  });

  it("returns 502 when Bridge is unreachable", async () => {
    mockFetch.mockRejectedValue(new Error("Connection refused"));

    const req = makeRequest("GET", "health");
    const res = await GET(req, makeContext(["health"]));

    expect(res.status).toBe(502);
    const body = await res.json();
    expect(body.error).toContain("Bridge");
  });

  it("passes through response status codes", async () => {
    mockFetch.mockResolvedValue({
      ok: false,
      status: 404,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ error: "Not found" }),
    });

    const req = makeRequest("GET", "api/people/invalid");
    const res = await GET(req, makeContext(["api", "people", "invalid"]));

    expect(res.status).toBe(404);
  });
});

// This proxy forwards ANY bridge path under the operator's own session, so a
// vault read reached the browser through it. The bridge now refuses human
// sessions on /api/vault/get, and the browser has no business asking: deny the
// path here too, so neither side is the only thing standing between an XSS on
// the Helm and a credential dump.
describe("Bridge Proxy vault denylist", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const denied: [string, string[]][] = [
    ["api/vault/get", ["api", "vault", "get"]],
    ["api/vault/list", ["api", "vault", "list"]],
    // The bridge mounts these under /api/, but the proxy accepts a bare path
    // too — deny both spellings rather than the one we happen to call.
    ["vault/get", ["vault", "get"]],
    ["vault/list", ["vault", "list"]],
    // Denylists that match the raw string get walked around; this one is
    // applied to the resolved target path.
    ["api/people/../vault/get", ["api", "people", "..", "vault", "get"]],
    ["api/VAULT/get", ["api", "VAULT", "get"]],
  ];

  it.each(denied)("refuses %s with 404 and never forwards", async (path, segments) => {
    const res = await GET(makeRequest("GET", path), makeContext(segments));

    expect(res.status).toBe(404);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("refuses a vault path on a mutating method too", async () => {
    const post = await POST(
      makeRequest("POST", "api/vault/get", "{}"),
      makeContext(["api", "vault", "get"])
    );
    const del = await DELETE(
      makeRequest("DELETE", "api/vault/list"),
      makeContext(["api", "vault", "list"])
    );

    expect(post.status).toBe(404);
    expect(del.status).toBe(404);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("still forwards other vault-adjacent bridge paths", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ ok: true }),
    });

    const res = await GET(
      makeRequest("GET", "api/vault-status"),
      makeContext(["api", "vault-status"])
    );

    expect(res.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledWith(
      "http://127.0.0.1:9100/api/vault-status",
      expect.any(Object)
    );
  });
});
