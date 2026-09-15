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

/**
 * The audit CSV export is a plain `<a href download>` on the Helm's Audit page,
 * which means the BROWSER, not the app, decides what to do with the reply — and
 * it decides from the headers. This proxy read the body and rebuilt the
 * response, so `Content-Type: text/csv` and the `Content-Disposition` naming
 * `audit-<tenant>-<date>.csv` were both dropped: the export opened as a wall of
 * text in a tab instead of saving as a file, on a route whose entire purpose is
 * handing an auditor a file.
 *
 * Forwarded by allowlist, not wholesale: `Set-Cookie` and the bridge's own auth
 * headers have no business crossing back into the browser.
 */
describe("Bridge Proxy attachment headers", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  function csvReply() {
    return {
      ok: true,
      status: 200,
      headers: new Headers({
        "content-type": "text/csv; charset=utf-8",
        "content-disposition": 'attachment; filename="audit-acme-2026-09-15.csv"',
        "set-cookie": "bridge_session=secret; Path=/",
      }),
      text: () => Promise.resolve("id,timestamp\r\n1,2026-09-15T12:00:00+00:00\r\n"),
      json: () => Promise.reject(new Error("not json")),
    };
  }

  it("passes Content-Disposition and Content-Type through for a CSV export", async () => {
    mockFetch.mockResolvedValue(csvReply());

    const res = await GET(
      makeRequest("GET", "api/audit/events.csv"),
      makeContext(["api", "audit", "events.csv"])
    );

    expect(res.status).toBe(200);
    expect(res.headers.get("content-disposition")).toBe(
      'attachment; filename="audit-acme-2026-09-15.csv"'
    );
    expect(res.headers.get("content-type")).toContain("text/csv");
    expect(await res.text()).toContain("id,timestamp");
  });

  /**
   * Forwarding the bridge's `Content-Type` verbatim is what makes the download
   * work; it also means a same-origin path a browser can navigate to now
   * renders whatever type the bridge names. Nothing on the bridge echoes a
   * caller-influenced type today — the CSV export is the only non-JSON
   * producer — but `nosniff` costs one header and takes the question away.
   */
  it("tells the browser not to sniff a forwarded type", async () => {
    mockFetch.mockResolvedValue(csvReply());

    const res = await GET(
      makeRequest("GET", "api/audit/events.csv"),
      makeContext(["api", "audit", "events.csv"])
    );

    expect(res.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("does not pass the bridge's cookies back to the browser", async () => {
    mockFetch.mockResolvedValue(csvReply());

    const res = await GET(
      makeRequest("GET", "api/audit/events.csv"),
      makeContext(["api", "audit", "events.csv"])
    );

    expect(res.headers.get("set-cookie")).toBeNull();
  });

  it("leaves a JSON reply exactly as it was", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ units: [], available: false }),
    });

    const res = await GET(
      makeRequest("GET", "api/logs/units"),
      makeContext(["api", "logs", "units"])
    );

    expect(res.headers.get("content-type")).toContain("application/json");
    expect(await res.json()).toEqual({ units: [], available: false });
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

  it("still forwards the approvals routes the Inbox answers on", async () => {
    // The Helm's Inbox is the only surface an operator has for a workflow
    // approval or an agent's question. A denylist that swallowed these would
    // leave every waiting run stuck with no visible cause.
    mockFetch.mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: () => Promise.resolve({ count: 0, pending: [] }),
    });

    const list = await GET(makeRequest("GET", "api/approvals"), makeContext(["api", "approvals"]));
    expect(list.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledWith(
      "http://127.0.0.1:9100/api/approvals",
      expect.any(Object)
    );

    const answer = await POST(
      makeRequest("POST", "api/approvals/question/abc", JSON.stringify({ answer: "Alice" })),
      makeContext(["api", "approvals", "question", "abc"])
    );
    expect(answer.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledWith(
      "http://127.0.0.1:9100/api/approvals/question/abc",
      expect.objectContaining({ method: "POST" })
    );
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
