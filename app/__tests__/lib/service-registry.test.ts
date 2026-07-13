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
import {
  getConfiguredServiceUrl,
  getServiceUrl,
  getHealthUrl,
  listServices,
  _resetCache,
} from "@/lib/services/registry";

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
  delete process.env.ROBOTHOR_ENGINE_URL;
  delete process.env.VISION_URL;
  delete process.env.ROBOTHOR_SERVICES_MANIFEST;
  delete process.env.ROBOTHOR_WORKSPACE;
});

afterEach(() => {
  _resetCache();
});

describe("getServiceUrl", () => {
  it("returns bridge URL from manifest", () => {
    expect(getServiceUrl("bridge")).toBe("http://127.0.0.1:9100");
  });

  it("uses the engine runtime override", () => {
    process.env.ROBOTHOR_ENGINE_URL = "http://engine:18800";
    expect(getServiceUrl("engine")).toBe("http://engine:18800");
  });

  it("returns orchestrator URL", () => {
    expect(getServiceUrl("orchestrator")).toBe("http://0.0.0.0:9099");
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

  it("fails closed instead of falling back when an override is invalid", () => {
    process.env.BRIDGE_URL = "file:///etc/passwd";
    expect(() => getServiceUrl("bridge")).toThrow(
      "Invalid BRIDGE_URL service URL configuration",
    );
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

  it("returns null for unknown service", () => {
    expect(getHealthUrl("nonexistent")).toBeNull();
  });
});

describe("getConfiguredServiceUrl", () => {
  it("uses only a validated runtime override", () => {
    process.env.BRIDGE_URL = "http://bridge:9100";
    expect(getConfiguredServiceUrl("bridge", "/ready")).toBe(
      "http://bridge:9100/ready",
    );
  });

  it("does not fall back to the filesystem manifest", () => {
    vi.mocked(fs.readFileSync).mockClear();
    expect(getConfiguredServiceUrl("bridge", "/ready")).toBeNull();
    expect(fs.readFileSync).not.toHaveBeenCalled();
  });

  it("rejects unsafe protocols and embedded credentials", () => {
    process.env.BRIDGE_URL = "file:///etc/passwd";
    expect(getConfiguredServiceUrl("bridge", "/ready")).toBeNull();

    process.env.BRIDGE_URL = "http://operator:secret@bridge:9100";
    expect(getConfiguredServiceUrl("bridge", "/ready")).toBeNull();
  });
});

describe("listServices", () => {
  it("returns all services", () => {
    const services = listServices();
    expect(Object.keys(services)).toContain("bridge");
    expect(Object.keys(services)).toContain("orchestrator");
    expect(Object.keys(services)).toContain("vision");
  });

  it("uses an explicitly configured manifest without searching the home directory", () => {
    process.env.ROBOTHOR_SERVICES_MANIFEST = "/run/genus/services.json";
    _resetCache();

    listServices();

    expect(fs.readFileSync).toHaveBeenCalledWith(
      "/run/genus/services.json",
      "utf-8"
    );
    expect(vi.mocked(fs.readFileSync).mock.calls.flat()).not.toContain(
      `${process.env.HOME}/robothor/robothor-services.json`
    );
  });
});
