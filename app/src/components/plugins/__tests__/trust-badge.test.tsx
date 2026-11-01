/**
 * The badge is one word and a button; the drawer behind it is every fact the
 * word was chosen from. Closed, the drawer renders nothing — the Plugins page
 * pins that its text never says "verdict", "safe" or "unscanned" for a row
 * nothing scanned, and a drawer mounted off-screen would break that quietly.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { pluginTrust } from "@/lib/plugins/trust";

import { TrustBadge } from "../trust-badge";

const VERIFIED = pluginTrust({
  name: "acme-tools",
  trust: {
    signature: { verified: true, key_id: "k1", index_url: "https://index.example/index.json" },
    wheel: { sha256: "b".repeat(64), pinned: true },
    manifest: { sha256: "c".repeat(64), matches: true },
    scan: { verdict: "safe", reasons: [], members_accounted: 7 },
    lockfile: { recorded: true, drifted: false },
    contract_version: 1,
  },
});

const REVIEW = pluginTrust({
  name: "acme-tools",
  trust: {
    signature: { verified: true, key_id: "k1", index_url: null },
    wheel: { sha256: "b".repeat(64), pinned: true },
    manifest: { sha256: "c".repeat(64), matches: true },
    scan: { verdict: "review", reasons: ["a.py:1: imports httpx", "b.py:2: opens a socket"] },
    lockfile: { recorded: true, drifted: false },
    contract_version: 1,
  },
});

describe("TrustBadge", () => {
  it("shows the level as a word and a data attribute", () => {
    render(<TrustBadge name="acme-tools" trust={VERIFIED} testId="t" />);
    const badge = screen.getByTestId("t");
    expect(badge).toHaveAttribute("data-level", "verified");
    expect(badge.textContent).toContain("Verified");
  });

  it("renders no drawer until it is opened", () => {
    render(<TrustBadge name="acme-tools" trust={VERIFIED} testId="t" />);
    expect(screen.queryByTestId("trust-drawer")).toBeNull();
    expect(document.body.textContent).not.toContain("k1");
  });

  it("opens a drawer with the key id, both hashes, the scan and the contract version", () => {
    render(<TrustBadge name="acme-tools" trust={VERIFIED} testId="t" />);
    fireEvent.click(screen.getByTestId("t"));
    const drawer = screen.getByTestId("trust-drawer");
    expect(drawer.textContent).toContain("acme-tools");
    expect(screen.getByTestId("trust-signature").textContent).toContain("k1");
    expect(screen.getByTestId("trust-wheel").textContent).toContain("b".repeat(64));
    expect(screen.getByTestId("trust-manifest").textContent).toContain("c".repeat(64));
    expect(screen.getByTestId("trust-manifest").textContent).toMatch(/matches/i);
    expect(screen.getByTestId("trust-scan").textContent).toContain("7");
    expect(screen.getByTestId("trust-contract").textContent).toContain("1");
  });

  it("lists every scan reason", () => {
    render(<TrustBadge name="acme-tools" trust={REVIEW} testId="t" />);
    fireEvent.click(screen.getByTestId("t"));
    const reasons = screen.getAllByTestId("trust-scan-reason").map((li) => li.textContent);
    expect(reasons).toEqual(["a.py:1: imports httpx", "b.py:2: opens a socket"]);
  });

  it("says plainly when nothing vouched for the plugin", () => {
    const unsigned = pluginTrust({ name: "x", verdict: "unscanned", recorded: true });
    render(<TrustBadge name="x" trust={unsigned} testId="t" />);
    fireEvent.click(screen.getByTestId("t"));
    expect(screen.getByTestId("trust-signature").textContent).toMatch(/not signed/i);
    expect(screen.getByTestId("trust-wheel").textContent).toMatch(/not pinned/i);
  });
});
