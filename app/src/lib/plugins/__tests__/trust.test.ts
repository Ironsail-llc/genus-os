/**
 * One trust answer per plugin, from what install and sync already recorded.
 *
 * The browser verifies nothing: the engine checked the index signature when it
 * parsed the index and the wheel's sha256 when it downloaded it, and wrote both
 * into the lock row. This module only READS that record and picks the one word
 * the badge shows, worst first: blocked, drifted, review, unsigned, not
 * scanned, verified.
 *
 * And it must keep working against an engine that predates `trust`: the old
 * fields (`verdict`, `drifted`, `recorded`, `source`, `manifest`) already say
 * most of it, and a badge that went blank on an older bridge would be the page
 * claiming less than it knows.
 */
import { describe, expect, it } from "vitest";

import { agentTrust, pluginTrust, TRUST_LABEL } from "../trust";

const SIGNED = {
  signature: { verified: true, key_id: "k1", index_url: "https://index.example/index.json" },
  wheel: { sha256: "b".repeat(64), pinned: true },
  manifest: { sha256: "c".repeat(64), matches: true },
  scan: { verdict: "safe", reasons: [], members_accounted: 7 },
  lockfile: { recorded: true, drifted: false },
  contract_version: 1,
};

function row(trust: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return { name: "acme-tools", verdict: "safe", drifted: false, recorded: true, trust, ...extra };
}

describe("pluginTrust — the badge level", () => {
  it("is verified when signed, pinned, matching and scanned safe", () => {
    const trust = pluginTrust(row(SIGNED));
    expect(trust.level).toBe("verified");
    expect(trust.signature).toEqual({
      verified: true,
      keyId: "k1",
      indexUrl: "https://index.example/index.json",
    });
    expect(trust.wheel).toEqual({ sha256: "b".repeat(64), pinned: true });
    expect(trust.scan.membersAccounted).toBe(7);
    expect(trust.contractVersion).toBe(1);
  });

  it("is unsigned when no index vouched for it", () => {
    const trust = pluginTrust(
      row({
        ...SIGNED,
        signature: { verified: false, key_id: null, index_url: null },
        scan: { verdict: "unscanned", reasons: [], members_accounted: null },
      })
    );
    expect(trust.level).toBe("unsigned");
  });

  it("is review when the scan found something the operator accepted, with its reasons", () => {
    const trust = pluginTrust(
      row({ ...SIGNED, scan: { verdict: "review", reasons: ["a.py:1: imports httpx"] } })
    );
    expect(trust.level).toBe("review");
    expect(trust.scan.reasons).toEqual(["a.py:1: imports httpx"]);
  });

  it("is blocked when the scan blocked it, and blocked outranks drift", () => {
    const trust = pluginTrust(
      row({
        ...SIGNED,
        scan: { verdict: "blocked", reasons: ["a.py:1: imports ctypes"] },
        manifest: { sha256: "c".repeat(64), matches: false },
        lockfile: { recorded: true, drifted: true },
      })
    );
    expect(trust.level).toBe("blocked");
  });

  it("is drifted when the manifest no longer matches, and drift outranks review and unsigned", () => {
    const trust = pluginTrust(
      row({
        ...SIGNED,
        signature: { verified: false, key_id: null, index_url: null },
        scan: { verdict: "review", reasons: [] },
        manifest: { sha256: "c".repeat(64), matches: false },
        lockfile: { recorded: true, drifted: true },
      })
    );
    expect(trust.level).toBe("drifted");
    expect(trust.manifest.matches).toBe(false);
  });

  it("is not scanned when signed but nothing scanned it", () => {
    const trust = pluginTrust(
      row({ ...SIGNED, scan: { verdict: "unscanned", reasons: [], members_accounted: null } })
    );
    expect(trust.level).toBe("unscanned");
  });

  it("has a label for every level that never says safe or unscanned", () => {
    for (const label of Object.values(TRUST_LABEL)) {
      expect(label).not.toMatch(/\bsafe\b/i);
      expect(label).not.toContain("unscanned");
    }
  });
});

describe("pluginTrust — an engine that sends no trust object", () => {
  it("reads the signature off source and the drift off drifted", () => {
    const trust = pluginTrust({
      name: "genus-weather",
      verdict: "review",
      drifted: false,
      recorded: true,
      source: { origin: "registry", publisher_key_id: "genus-2026", index_url: "https://i.example" },
      manifest: { contract_version: 1 },
    });
    expect(trust.signature).toEqual({
      verified: true,
      keyId: "genus-2026",
      indexUrl: "https://i.example",
    });
    expect(trust.level).toBe("review");
    expect(trust.contractVersion).toBe(1);
    expect(trust.wheel.pinned).toBe(false);
  });

  it("calls a sync-recorded row unsigned and a drifted one drifted", () => {
    expect(pluginTrust({ name: "x", verdict: "unscanned", recorded: true }).level).toBe("unsigned");
    expect(pluginTrust({ name: "x", verdict: "unscanned", drifted: true }).level).toBe("drifted");
  });

  it("survives garbage", () => {
    expect(pluginTrust(null).level).toBe("unsigned");
    expect(pluginTrust({ trust: "nope" }).level).toBe("unsigned");
  });
});

describe("agentTrust — a marketplace agent no plugin shipped", () => {
  it("is unsigned and carries the sha256 it was pinned to", () => {
    const trust = agentTrust({ agent_id: "note-taker", source: "hub", source_sha256: "d".repeat(64) });
    expect(trust.level).toBe("unsigned");
    expect(trust.wheel).toEqual({ sha256: "d".repeat(64), pinned: true });
  });

  it("is unpinned when nothing recorded a sha256", () => {
    expect(agentTrust({ agent_id: "local-one", source: "local" }).wheel.pinned).toBe(false);
  });
});
