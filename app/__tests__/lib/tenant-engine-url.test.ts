/**
 * Which engine a tenant's requests go to.
 *
 * Same order the bridge resolves it: an explicit per-tenant override, then a
 * template, then the single engine. The root tenant is never templated — it IS
 * the engine `ROBOTHOR_ENGINE_URL` names, and a template that resolved it to
 * `…-engine-default` would point the operator's own home at a Service that
 * does not exist.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { engineUrlFor } from "@/lib/services/registry";

const ROOT = "http://engine.root.test:18800";
const TEMPLATE = "http://genus-engine-{tenant}:18800";

describe("engineUrlFor", () => {
  beforeEach(() => {
    vi.stubEnv("ROBOTHOR_ENGINE_URL", ROOT);
    vi.stubEnv("GENUS_TENANT_ENGINE_URLS", "");
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "");
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("uses an explicit override before the template", () => {
    vi.stubEnv(
      "GENUS_TENANT_ENGINE_URLS",
      JSON.stringify({ "acme-child-a": "http://override.test:9000/" }),
    );
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor("acme-child-a")).toBe("http://override.test:9000");
  });

  it("fills the literal {tenant} placeholder of the template", () => {
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor("acme-child-a")).toBe("http://genus-engine-acme-child-a:18800");
  });

  it("uses the template for a tenant the override map does not name", () => {
    vi.stubEnv(
      "GENUS_TENANT_ENGINE_URLS",
      JSON.stringify({ "acme-child-b": "http://override.test:9000" }),
    );
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor("acme-child-a")).toBe("http://genus-engine-acme-child-a:18800");
  });

  it("falls back to ROBOTHOR_ENGINE_URL with neither override nor template", () => {
    expect(engineUrlFor("acme-child-a")).toBe(ROOT);
  });

  it("falls back to the local engine when nothing is configured", () => {
    vi.stubEnv("ROBOTHOR_ENGINE_URL", "");

    expect(engineUrlFor(null)).toBe("http://127.0.0.1:18800");
  });

  it.each([undefined, null, ""])("resolves no tenant (%s) to the root engine", (tenant) => {
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor(tenant)).toBe(ROOT);
  });

  it("always resolves the root tenant to ROBOTHOR_ENGINE_URL, even when overridden", () => {
    vi.stubEnv(
      "GENUS_TENANT_ENGINE_URLS",
      JSON.stringify({ default: "http://override.test:9000" }),
    );
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor("default")).toBe(ROOT);
  });

  it("reads the root tenant from ROBOTHOR_DEFAULT_TENANT", () => {
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "acme-parent");
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor("acme-parent")).toBe(ROOT);
    expect(engineUrlFor("default")).toBe("http://genus-engine-default:18800");
  });

  it("refuses a tenant id that is not a DNS label rather than splice it into a URL", () => {
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(() => engineUrlFor("evil.test/x?")).toThrow(/tenant/i);
    expect(() => engineUrlFor("alice@example.com")).toThrow(/tenant/i);
  });

  it("refuses a malformed override map instead of quietly routing to root", () => {
    vi.stubEnv("GENUS_TENANT_ENGINE_URLS", "{not json");

    expect(() => engineUrlFor("acme-child-a")).toThrow(/GENUS_TENANT_ENGINE_URLS/);
  });

  it("refuses an override or template that is not an http(s) URL", () => {
    vi.stubEnv("GENUS_TENANT_ENGINE_URLS", JSON.stringify({ "acme-child-a": "file:///etc/passwd" }));
    expect(() => engineUrlFor("acme-child-a")).toThrow();

    vi.stubEnv("GENUS_TENANT_ENGINE_URLS", "");
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", "ftp://engine-{tenant}");
    expect(() => engineUrlFor("acme-child-a")).toThrow();
  });
});
