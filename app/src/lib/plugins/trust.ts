/**
 * One trust answer per plugin, read from what install and sync recorded.
 *
 * Nothing is verified here. The engine checked the index signature when it
 * parsed the index and the wheel's sha256 against the index's pin before pip
 * ran, and wrote both into the lock row; `GET /api/plugins` hands that record
 * back as `trust`. This module reads it and picks the one word a badge shows,
 * worst first: blocked, drifted, review, unsigned, not scanned, verified.
 *
 * An engine that predates `trust` still sends `verdict`, `drifted`, `recorded`,
 * `source` and `manifest`, which say most of it, so the answer is derived from
 * those rather than left blank. Anything unreadable degrades to "unsigned",
 * never to "verified".
 */

export type TrustLevel = "verified" | "unscanned" | "unsigned" | "review" | "drifted" | "blocked";

export interface Trust {
  level: TrustLevel;
  signature: { verified: boolean; keyId: string | null; indexUrl: string | null };
  /** For an agent no plugin shipped, the bundle's pinned sha256. */
  wheel: { sha256: string | null; pinned: boolean };
  /** `matches` is null when nothing was recorded to match against. */
  manifest: { sha256: string | null; matches: boolean | null };
  scan: { verdict: string | null; reasons: string[]; membersAccounted: number | null };
  lockfile: { recorded: boolean; drifted: boolean };
  contractVersion: number | null;
}

/**
 * What each level is called on screen. Never the scan's own words: "safe" on a
 * badge reads as a promise, and "unscanned" is the placeholder the Plugins page
 * has always refused to print.
 */
export const TRUST_LABEL: Record<TrustLevel, string> = {
  verified: "Verified",
  unscanned: "Not scanned",
  unsigned: "Unsigned",
  review: "Review",
  drifted: "Drifted",
  blocked: "Blocked",
};

type Raw = Record<string, unknown>;

function obj(value: unknown): Raw {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Raw) : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Worst first. Blocked outranks drift: a blocked wheel is refused however it hashes. */
function levelOf(facts: Omit<Trust, "level">): TrustLevel {
  const verdict = facts.scan.verdict;
  if (verdict === "blocked") return "blocked";
  if (facts.lockfile.drifted || facts.manifest.matches === false) return "drifted";
  if (verdict === "review") return "review";
  if (!facts.signature.verified) return "unsigned";
  if (verdict !== "safe") return "unscanned";
  return "verified";
}

function withLevel(facts: Omit<Trust, "level">): Trust {
  return { level: levelOf(facts), ...facts };
}

/** From one row of `GET /api/plugins`, with or without its `trust` object. */
export function pluginTrust(value: unknown): Trust {
  const row = obj(value);
  const trust = obj(row.trust);
  const manifest = obj(row.manifest);
  const contract = typeof manifest.contract_version === "number" ? manifest.contract_version : null;

  if (Object.keys(trust).length > 0) {
    const signature = obj(trust.signature);
    const wheel = obj(trust.wheel);
    const digest = obj(trust.manifest);
    const scan = obj(trust.scan);
    const lock = obj(trust.lockfile);
    const wheelSha = text(wheel.sha256);
    return withLevel({
      signature: {
        verified: signature.verified === true,
        keyId: signature.verified === true ? text(signature.key_id) : null,
        indexUrl: signature.verified === true ? text(signature.index_url) : null,
      },
      wheel: { sha256: wheelSha, pinned: wheel.pinned === true && wheelSha !== null },
      manifest: {
        sha256: text(digest.sha256),
        matches: typeof digest.matches === "boolean" ? digest.matches : null,
      },
      scan: {
        verdict: text(scan.verdict),
        reasons: texts(scan.reasons),
        membersAccounted: count(scan.members_accounted),
      },
      lockfile: { recorded: lock.recorded === true, drifted: lock.drifted === true },
      contractVersion:
        typeof trust.contract_version === "number" ? trust.contract_version : contract,
    });
  }

  // An older engine: the same facts, minus the hashes it never sent.
  const source = obj(row.source);
  const keyId = text(source.publisher_key_id);
  const signed = source.origin === "registry" && keyId !== null;
  const recorded = row.recorded === true;
  const drifted = row.drifted === true;
  return withLevel({
    signature: {
      verified: signed,
      keyId: signed ? keyId : null,
      indexUrl: signed ? text(source.index_url) : null,
    },
    wheel: { sha256: null, pinned: false },
    manifest: { sha256: null, matches: recorded ? !drifted : null },
    scan: { verdict: text(row.verdict), reasons: [], membersAccounted: null },
    lockfile: { recorded, drifted },
    contractVersion: contract,
  });
}

/**
 * From one row of `GET /api/installed-agents` that no plugin shipped. No index
 * vouches for a hub or local bundle, so it is unsigned; the sha256 it was
 * pinned to at install is the one fact there is.
 */
export function agentTrust(value: unknown): Trust {
  const row = obj(value);
  const sha = text(row.source_sha256);
  return withLevel({
    signature: { verified: false, keyId: null, indexUrl: null },
    wheel: { sha256: sha, pinned: sha !== null },
    manifest: { sha256: null, matches: null },
    scan: { verdict: null, reasons: [], membersAccounted: null },
    lockfile: { recorded: false, drifted: false },
    contractVersion: null,
  });
}
