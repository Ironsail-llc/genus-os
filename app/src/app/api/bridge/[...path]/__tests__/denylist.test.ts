/**
 * What this proxy refuses, and — just as load-bearing — what it must not.
 *
 * The denylist exists because `/api/vault/get` once answered an owner session
 * with a decrypted credential, so it is deliberately blunt. Blunt patterns
 * over-match: the provider routes are the appliance's credential *write* path
 * and read back nothing but digests, and a denylist edit that swallowed them
 * would take Settings and the setup wizard offline with a 404 that looks like
 * a missing route rather than a policy decision.
 */
import { describe, expect, it } from "vitest";

import { isDeniedBridgePath } from "@/lib/bridge-proxy-policy";

describe("bridge proxy denylist", () => {
  it("refuses vault reads in either mount shape", () => {
    for (const path of [
      "/api/vault/get",
      "/vault/get",
      "/api/vault/list",
      "/api/vault/get/OPENROUTER_API_KEY",
      "/API/VAULT/GET",
    ]) {
      expect(isDeniedBridgePath(path)).toBe(true);
    }
  });

  it("does not refuse the operator provider routes", () => {
    for (const path of [
      "/api/providers",
      "/api/providers/openrouter/keys/1",
      "/api/providers/openrouter/test",
      "/api/providers/defaults",
      "/api/models",
    ]) {
      expect(isDeniedBridgePath(path)).toBe(false);
    }
  });

  it("still allows vault writes, which carry no secret back", () => {
    for (const path of ["/api/vault/set", "/api/vault/delete"]) {
      expect(isDeniedBridgePath(path)).toBe(false);
    }
  });

  it("refuses the first-run setup routes, which must never carry a session", () => {
    // This proxy attaches the browser's bridge token to everything it
    // forwards. The setup routes create the owner account and authenticate
    // with a short-lived claim token instead; reaching them with a session is
    // precisely what that design refuses, so they do not come through here.
    for (const path of [
      "/api/setup",
      "/api/setup/status",
      "/api/setup/claim",
      "/api/setup/operator",
      "/setup/operator",
      "/API/SETUP/COMPLETE",
    ]) {
      expect(isDeniedBridgePath(path)).toBe(true);
    }
  });

  it("does not refuse a route that merely starts with the same letters", () => {
    for (const path of ["/api/setups", "/api/setup-wizard", "/api/settings"]) {
      expect(isDeniedBridgePath(path)).toBe(false);
    }
  });
});
