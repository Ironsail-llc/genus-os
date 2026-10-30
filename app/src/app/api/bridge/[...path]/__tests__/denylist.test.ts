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

  it("does not refuse the agent-manifest routes", () => {
    // The agent builder lives entirely behind this proxy: the browser reads the
    // fleet, validates as the operator types, and saves through these paths. A
    // denylist edit that swallowed them would take the whole builder offline
    // with a 404 that reads as a missing route rather than a policy decision.
    // Nothing here answers with a credential — a manifest is configuration, and
    // secret values in one are `${VAR}` references the engine expands at load.
    for (const path of [
      "/api/agent-manifests",
      "/api/agent-manifests/demo-agent",
      "/api/agent-manifests/validate",
      "/api/agent-manifests/demo-agent/enable",
      "/api/agent-manifests/demo-agent/disable",
      "/api/agent-manifests/demo-agent/run",
    ]) {
      expect(isDeniedBridgePath(path)).toBe(false);
    }
  });

  it("does not refuse the settings routes the Config and Flags pages live on", () => {
    // Settings › Config reads the schema and the values and PATCHes changes
    // through this proxy, and Settings › Flags reads and writes the governed
    // controls through it. `/api/settings` shares four letters with
    // `/api/setup`, which IS denied, so this is the pair a blunt pattern
    // confuses — and the failure would be a 404 that reads as a missing route
    // rather than as a policy decision. Nothing here answers with a
    // credential: the settings route projects every secret down to
    // `{configured, fingerprint}` before it serializes.
    for (const path of [
      "/api/settings",
      "/api/settings/schema",
      "/settings",
      "/api/controls",
      "/api/controls/ROBOTHOR_RBAC_MODE",
    ]) {
      expect(isDeniedBridgePath(path)).toBe(false);
    }
  });

  it("does not refuse the plugin routes the Plugins page lives on", () => {
    // The Plugins page reads what is installed and turns one off through this
    // proxy. Nothing here answers with a credential — a plugin listing is a
    // distribution name, a version and a load state, and the engine's own
    // response deliberately carries no filesystem path at all.
    for (const path of [
      "/api/plugins",
      "/api/plugins/sync",
      "/api/plugins/reload",
      "/api/plugins/genus-hostinfo/enable",
      "/api/plugins/genus-hostinfo/disable",
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
