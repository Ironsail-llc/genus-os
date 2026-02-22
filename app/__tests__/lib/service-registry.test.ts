/**
 * Tests for TypeScript service registry client.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Must mock fs before importing
vi.mock("fs", () => ({
  default: {
    readFileSync: vi.fn(),
  },
}));

import fs from "fs";
import { getServiceUrl, getHealthUrl, listServices, _resetCache } from "@/lib/services/registry";

const mockManifest = {
  version: "1.0.0",
  services: {
    bridge: {
      name: "Bridge Service",
      port: 9100,
      host: "127.0.0.1",
      health: "/health",
      dependencies: ["postgres", "redis"],
    },
    orchestrator: {
      name: "RAG Orchestrator",
      port: 9099,
      host: "0.0.0.0",
      health: "/health",
      dependencies: ["postgres"],
    },
    gateway: {
      name: "OpenClaw Gateway",
      port: 18789,
      host: "0.0.0.0",
      health: null,
      protocol: "ws",
      dependencies: [],
    },
    vision: {
      name: "Vision Service",
      port: 8600,
      host: "0.0.0.0",
      health: "/health",
      dependencies: [],
    },
  },
};

beforeEach(() => {
  _resetCache();
  vi.mocked(fs.readFileSync).mockReturnValue(JSON.stringify(mockManifest));
  // Clear env overrides
  delete process.env.BRIDGE_URL;
  delete process.env.ORCHESTRATOR_URL;
  delete process.env.VISION_URL;
  delete process.env.GATEWAY_URL;
});

afterEach(() => {
  _resetCache();
});

describe("getServiceUrl", () => {
  it("returns bridge URL from manifest", () => {
    expect(getServiceUrl("bridge")).toBe("http://127.0.0.1:9100");
  });

  it("returns orchestrator URL", () => {
    expect(getServiceUrl("orchestrator")).toBe("http://0.0.0.0:9099");
  });

  it("returns websocket URL for gateway", () => {
    expect(getServiceUrl("gateway")).toBe("ws://0.0.0.0:18789");
  });

  it("appends path to URL", () => {
    expect(getServiceUrl("bridge", "/api/people")).toBe("http://127.0.0.1:9100/api/people");
  });

  it("returns null for unknown service", () => {
    expect(getServiceUrl("nonexistent")).toBeNull();
  });

  it("uses env override when set", () => {
    process.env.BRIDGE_URL = "http://custom:9999";
    _resetCache();
    expect(getServiceUrl("bridge")).toBe("http://custom:9999");
  });

  it("uses env override with path", () => {
    process.env.BRIDGE_URL = "http://custom:9999";
    _resetCache();
    expect(getServiceUrl("bridge", "/health")).toBe("http://custom:9999/health");
  });

  it("strips trailing slash from env override", () => {
    process.env.BRIDGE_URL = "http://custom:9999/";
    _resetCache();
    expect(getServiceUrl("bridge", "/health")).toBe("http://custom:9999/health");
  });
});

describe("getHealthUrl", () => {
  it("returns health URL for bridge", () => {
    expect(getHealthUrl("bridge")).toBe("http://127.0.0.1:9100/health");
  });

  it("returns null when no health endpoint", () => {
    expect(getHealthUrl("gateway")).toBeNull();
  });

  it("returns null for unknown service", () => {
    expect(getHealthUrl("nonexistent")).toBeNull();
  });
});

describe("listServices", () => {
  it("returns all services", () => {
    const services = listServices();
    expect(Object.keys(services)).toContain("bridge");
    expect(Object.keys(services)).toContain("orchestrator");
    expect(Object.keys(services)).toContain("gateway");
  });
});
