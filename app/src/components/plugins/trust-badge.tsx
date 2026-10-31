"use client";

import { useState } from "react";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { TRUST_LABEL, type Trust, type TrustLevel } from "@/lib/plugins/trust";

/**
 * One word for how far a plugin can be trusted, and a drawer with every fact
 * the word was chosen from. The badge is a button; the drawer mounts only when
 * opened, so a closed one adds no text to the page it sits on.
 *
 * Read-only by construction: every fact comes from the engine's record of the
 * install (`lib/plugins/trust.ts`). Nothing here checks a signature or hashes
 * anything.
 */

const TONE: Record<TrustLevel, string> = {
  verified: "border-success/30 bg-success/10 text-success",
  unscanned: "border-border bg-muted text-muted-foreground",
  unsigned: "border-border bg-muted text-muted-foreground",
  review: "border-warning/30 bg-warning/10 text-warning",
  drifted: "border-warning/30 bg-warning/10 text-warning",
  blocked: "border-destructive/30 bg-destructive/10 text-destructive",
};

export interface TrustBadgeProps {
  /** What the drawer is about: a distribution name or an agent id. */
  name: string;
  trust: Trust;
  testId: string;
}

export function TrustBadge({ name, trust, testId }: TrustBadgeProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        data-testid={testId}
        data-level={trust.level}
        aria-label={`Trust details for ${name}: ${TRUST_LABEL[trust.level]}`}
        onClick={() => setOpen(true)}
        className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium hover:opacity-80 ${TONE[trust.level]}`}
      >
        {TRUST_LABEL[trust.level]}
      </button>
      {open ? (
        <Sheet open onOpenChange={setOpen}>
          <SheetContent data-testid="trust-drawer" className="overflow-y-auto">
            <SheetHeader>
              <SheetTitle className="break-all">{name}</SheetTitle>
              <SheetDescription>
                {TRUST_LABEL[trust.level]}. What the engine recorded when this was installed or
                last recorded; nothing here is re-checked in the browser.
              </SheetDescription>
            </SheetHeader>
            <TrustFacts trust={trust} />
          </SheetContent>
        </Sheet>
      ) : null}
    </>
  );
}

function Fact({ label, testId, children }: { label: string; testId: string; children: React.ReactNode }) {
  return (
    <div data-testid={testId} className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 break-all text-xs text-foreground">{children}</dd>
    </div>
  );
}

function TrustFacts({ trust }: { trust: Trust }) {
  const { signature, wheel, manifest, scan, lockfile, contractVersion } = trust;
  return (
    <dl className="flex flex-col gap-3 px-4 pb-4">
      <Fact label="Index signature" testId="trust-signature">
        {signature.verified ? (
          <>
            Verified, signed by key <code>{signature.keyId}</code>
            {signature.indexUrl ? (
              <>
                {" "}
                from <code>{signature.indexUrl}</code>
              </>
            ) : null}
          </>
        ) : (
          "Not signed: no pinned index key vouched for this."
        )}
      </Fact>
      <Fact label="Artifact sha256" testId="trust-wheel">
        {wheel.pinned ? (
          <>
            Pinned, matched at install: <code>{wheel.sha256}</code>
          </>
        ) : (
          "Not pinned: no artifact hash was recorded."
        )}
      </Fact>
      <Fact label="Manifest sha256" testId="trust-manifest">
        {manifest.matches === null ? (
          "Not recorded."
        ) : manifest.matches ? (
          <>
            Matches the recorded value
            {manifest.sha256 ? (
              <>
                : <code>{manifest.sha256}</code>
              </>
            ) : null}
          </>
        ) : (
          <>
            Changed since it was recorded
            {manifest.sha256 ? (
              <>
                {" "}
                (recorded <code>{manifest.sha256}</code>)
              </>
            ) : null}
            ; the engine refuses to load it until it is recorded again.
          </>
        )}
      </Fact>
      <Fact label="Install scan" testId="trust-scan">
        {scan.verdict && scan.verdict !== "unscanned" ? (
          <>
            {scan.verdict}
            {scan.membersAccounted !== null
              ? `, ${scan.membersAccounted} wheel members accounted for`
              : null}
            {scan.reasons.length > 0 ? (
              <ul className="mt-1 list-disc pl-4">
                {scan.reasons.map((reason, index) => (
                  <li key={`${index}-${reason}`} data-testid="trust-scan-reason">
                    {reason}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : (
          "Not scanned: no install scan was recorded for this."
        )}
      </Fact>
      <Fact label="Lockfile" testId="trust-lockfile">
        {lockfile.recorded
          ? lockfile.drifted
            ? "Recorded; drifted from the record."
            : "Recorded; no drift."
          : "Not recorded."}
      </Fact>
      <Fact label="Contract version" testId="trust-contract">
        {contractVersion !== null ? String(contractVersion) : "Not declared."}
      </Fact>
    </dl>
  );
}
