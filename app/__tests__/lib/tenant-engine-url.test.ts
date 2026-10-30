/**
 * Which engine a session's requests go to — the tenant in its token, whether
 * a root admin is acting there or it is a child-tenant user's own home.
 *
 * Three steps, the same three-step order the bridge adopts: an explicit
 * per-tenant override, then a template, then the single engine. The root tenant is never templated — it IS
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
    vi.stubEnv("ROBOTHOR_TENANT_ENGINE_URLS", "");
    vi.stubEnv("ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE", "");
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "");
    vi.stubEnv("ROBOTHOR_PLATFORM_TENANT", "");
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

  // The bridge's settings accept the older ROBOTHOR_ names too; the dashboard
  // must route the same way whichever name an install uses.
  it("reads ROBOTHOR_TENANT_ENGINE_URLS when the GENUS_ name is unset", () => {
    vi.stubEnv(
      "ROBOTHOR_TENANT_ENGINE_URLS",
      JSON.stringify({ "acme-child-a": "http://alias-override.test:9000" }),
    );

    expect(engineUrlFor("acme-child-a")).toBe("http://alias-override.test:9000");
  });

  it("reads ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE when the GENUS_ name is unset", () => {
    vi.stubEnv("ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE", "http://alias-engine-{tenant}:18800");

    expect(engineUrlFor("acme-child-a")).toBe("http://alias-engine-acme-child-a:18800");
  });

  it("prefers the GENUS_ names over their ROBOTHOR_ aliases", () => {
    vi.stubEnv(
      "GENUS_TENANT_ENGINE_URLS",
      JSON.stringify({ "acme-child-a": "http://override.test:9000" }),
    );
    vi.stubEnv(
      "ROBOTHOR_TENANT_ENGINE_URLS",
      JSON.stringify({ "acme-child-a": "http://alias-override.test:9000" }),
    );
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);
    vi.stubEnv("ROBOTHOR_TENANT_ENGINE_URL_TEMPLATE", "http://alias-engine-{tenant}:18800");

    expect(engineUrlFor("acme-child-a")).toBe("http://override.test:9000");
    expect(engineUrlFor("acme-child-b")).toBe("http://genus-engine-acme-child-b:18800");
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

  // The bridge gates and routes on PLATFORM_TENANT, which is
  // ROBOTHOR_PLATFORM_TENANT when set and ROBOTHOR_DEFAULT_TENANT otherwise.
  // The dashboard must agree, or a root user's token (tid = the platform
  // tenant) is templated into a Service that does not exist.
  it("reads the root tenant from ROBOTHOR_PLATFORM_TENANT before ROBOTHOR_DEFAULT_TENANT", () => {
    vi.stubEnv("ROBOTHOR_PLATFORM_TENANT", "acme-root");
    vi.stubEnv("ROBOTHOR_DEFAULT_TENANT", "default");
    vi.stubEnv("GENUS_TENANT_ENGINE_URL_TEMPLATE", TEMPLATE);

    expect(engineUrlFor("acme-root")).toBe(ROOT);
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
