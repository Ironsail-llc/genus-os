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
});
