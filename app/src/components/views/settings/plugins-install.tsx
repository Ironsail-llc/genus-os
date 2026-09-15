"use client";

import { useCallback, useState } from "react";
import { Copy, Download } from "lucide-react";

import { Button } from "@/components/ui/button";
import { readBridgeReply } from "@/lib/bridge/read-reply";
import { BRIDGE_UNREACHABLE } from "@/lib/bridge/use-bridge-poll";

/**
 * Settings › Plugins › Install from the registry.
 *
 * Two posts to one route: Preview is `dry_run: true`, Install is the same body
 * with `dry_run: false`. Everything an operator needs to decide is in the first
 * answer — the artifact, its hash, whose key signed the index that pinned it,
 * and every sentence the static scan produced.
 *
 * Five rules, each of which the route's shape makes easy to get wrong:
 *
 * **`review` is the normal outcome, not an error.** `safe` means "contributes
 * tools and touches nothing outside this process". Anything importing `os`, or
 * whose manifest omits `entry_points:`, is `review` — which is most real
 * plugins. So this is built as *plan → read the findings → accept them out
 * loud → install*, and the acceptance is a checkbox the operator has to tick,
 * not a confirm dialog they can click through.
 *
 * **The verdict is a word, never a tick.** A checkmark beside a name is a
 * clearance, and what the scanner offers is much narrower: it reads the wheel's
 * source without executing it, it is explicitly not a sandbox, and
 * `docs/PLUGINS.md` lists what still gets past it. The word is the claim; a
 * symbol would be a bigger one.
 *
 * **`blocked` gets no button at all.** Not a disabled Install with an
 * explanation beside it — a control that looks like it might work if you tried
 * harder. The card says the plan will not be installed and shows why.
 *
 * **No URL box.** The index is a choice between what the operator configured
 * (`ROBOTHOR_PLUGIN_INDEXES`), and the control only exists when there is more
 * than one. A browser that can type a URL is a browser that can make the engine
 * fetch on a caller's say-so. Installing a wheel from a path is CLI-only for
 * the same reason, and the card says where that lives instead of hiding it.
 *
 * **A plan is an answer about one request.** Editing the name, the version or
 * the index retires it, because an acceptance carried over to a different wheel
 * is an acceptance of findings nobody read.
 */

const BRIDGE = "/api/bridge";

/** The engine answered nothing at all. A bridge that cannot reach it says 502. */
const ENGINE_UNREACHABLE =
  "The engine is unreachable from the bridge, so nothing was installed. Nothing has been changed.";

export interface InstallPlan {
  name: string;
  version: string;
  origin: string;
  indexUrl: string;
  publisherKeyId: string;
  filename: string;
  sha256: string;
  size: number;
  summary: string;
  verdict: string;
  reasons: string[];
  promptScan: string;
  groups: string[];
  filesScanned: number;
  membersAccounted: number;
}

/** The lock row an install recorded — the governance record that was written. */
export interface InstalledRow {
  name: string;
  version: string;
  verdict: string;
  kinds: string[];
  membersAccounted: number;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

export function normalizePlan(value: unknown): InstallPlan | null {
  if (!value || typeof value !== "object") return null;
  const plan = value as Record<string, unknown>;
  if (!text(plan.name)) return null;
  return {
    name: text(plan.name),
    version: text(plan.version),
    origin: text(plan.origin),
    indexUrl: text(plan.index_url),
    publisherKeyId: text(plan.publisher_key_id),
    filename: text(plan.filename),
    sha256: text(plan.sha256),
    size: count(plan.size),
    summary: text(plan.summary),
    /*
      An UNKNOWN verdict is treated as `blocked`, not as "not blocked".

      The engine refuses anything outside the three at parse time, so this is
      only reachable from a build mismatch — but the failure mode of the other
      default is a page offering Install for a judgement it could not read.
    */
    verdict: ["safe", "review", "blocked"].includes(text(plan.verdict))
      ? text(plan.verdict)
      : "blocked",
    reasons: strings(plan.reasons),
    promptScan: text(plan.prompt_scan),
    groups: strings(plan.groups),
    filesScanned: count(plan.files_scanned),
    membersAccounted: count(plan.members_accounted),
  };
}

function normalizeRow(value: unknown): InstalledRow | null {
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (!text(row.name)) return null;
  return {
    name: text(row.name),
    version: text(row.version),
    verdict: text(row.verdict),
    kinds: strings(row.kinds),
    membersAccounted: count(row.members_accounted),
  };
}

/** What the pill says, and how it is coloured. Never a symbol. */
function verdictPill(verdict: string): { label: string; className: string } {
  if (verdict === "safe") {
    return { label: "safe", className: "border-success/30 bg-success/10 text-success" };
  }
  if (verdict === "blocked") {
    return {
      label: "blocked",
      className: "border-destructive/30 bg-destructive/10 text-destructive",
    };
  }
  // The common one. Warning, not destructive: it is a decision to make, not a
  // fault to fix, and drawing the ordinary case in red teaches people to
  // ignore red.
  return { label: "review", className: "border-warning/30 bg-warning/10 text-warning" };
}

export interface PluginInstallCardProps {
  /** Index URLs this instance reads, in order. A picker appears above one. */
  indexes: string[];
  /** Retire whatever the page's other acts are saying, before this one speaks. */
  onAct: () => void;
  /** A row was written and the engine has not been told: raise the reload bar. */
  onInstalled: () => void;
}

export function PluginInstallCard({ indexes, onAct, onInstalled }: PluginInstallCardProps) {
  /*
    Only an index the route would accept may be offered.

    `configured_indexes()` returns whatever `ROBOTHOR_PLUGIN_INDEXES` holds,
    and both the bridge and the engine answer 422 to a non-https `index`. A
    picker that listed one would be offering a choice that cannot work, so they
    are filtered out here and named below instead of disappearing silently —
    an operator whose only configured index is `http://` needs to know why the
    control is missing, not to conclude the feature is broken.
  */
  const usable = indexes.filter((url) => url.startsWith("https://"));
  const refused = indexes.filter((url) => !url.startsWith("https://"));

  const [name, setName] = useState("");
  const [version, setVersion] = useState("");
  const [index, setIndex] = useState("");
  const [plan, setPlan] = useState<InstallPlan | null>(null);
  /**
   * The sha256 of the plan whose findings the operator accepted, or null.
   *
   * A BOOLEAN was the Critical: it survived a second Preview, so plan A's
   * acceptance armed Install for plan B's findings — and re-Previewing is the
   * likelier gesture, because `version` left blank means "latest" and a second
   * look is how an operator re-checks before committing. Keyed on the hash, an
   * acceptance cannot be spent on a different wheel even if some future path
   * forgets to clear it: the checkbox, the button and the request body all ask
   * the same question, "is this the plan that was accepted".
   */
  const [acceptedSha, setAcceptedSha] = useState<string | null>(null);
  const [busy, setBusy] = useState<"preview" | "install" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [row, setRow] = useState<InstalledRow | null>(null);
  const [note, setNote] = useState("");
  /** True once a real install has been ANSWERED, however it was answered. */
  const [attempted, setAttempted] = useState(false);
  /** Which hash is on the clipboard, so "copied" cannot outlive its plan. */
  const [copiedSha, setCopiedSha] = useState<string | null>(null);

  /*
    Any edit to what would be POSTed retires the plan and the acceptance with
    it. A plan answers one request; carried across a change it becomes an
    operator accepting findings about a wheel they are no longer installing.
  */
  const changed = useCallback(() => {
    setPlan(null);
    setAcceptedSha(null);
    setRow(null);
    setNote("");
    setAttempted(false);
    setCopiedSha(null);
    setError(null);
  }, []);

  const post = useCallback(
    async (dryRun: boolean) => {
      setBusy(dryRun ? "preview" : "install");
      setError(null);
      setRow(null);
      setNote("");
      if (dryRun) {
        // A new plan is a new question. The acceptance goes with the old one,
        // and so does a "copied" label about the old one's hash.
        setAcceptedSha(null);
        setAttempted(false);
        setCopiedSha(null);
      }
      onAct();
      try {
        const res = await fetch(`${BRIDGE}/api/plugins/install`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: name.trim(),
            version: version.trim() || undefined,
            // The index the PICKER is showing, whenever there is a choice to
            // show. Omitting it is not equivalent: the installer searches the
            // named index alone, and every configured index in order when none
            // is named — so a select displaying one while the engine resolved
            // the wheel from another would be a provenance claim that is not
            // true.
            index: usable.length > 1 ? index || usable[0] : undefined,
            /*
              Only on a real install, and only for THIS plan.

              A dry run has nothing to accept — the findings do not exist yet —
              so sending the field at all is a preview claiming a decision. And
              the acceptance is bound to the plan's hash, so a stale one cannot
              be spent on a wheel whose reasons were never displayed.
            */
            accept_review: dryRun ? undefined : plan !== null && acceptedSha === plan.sha256,
            dry_run: dryRun,
          }),
        });
        if (!dryRun) setAttempted(true);
        if (!res.ok) {
          /*
            The server's own sentence, whatever the status.

            A 504 here does NOT mean "failed": the engine stopped waiting at its
            60 s cap and says the operation may still be running. A page that
            rendered it as a failure would send an operator to retry an install
            that is in flight, and `pip install` twice over is how a half-written
            distribution happens. The route's own words say what to do instead.
          */
          setError(res.status === 502 ? ENGINE_UNREACHABLE : await readBridgeReply(res));
          if (dryRun) setPlan(null);
          return;
        }
        const body = (await res.json()) as Record<string, unknown>;
        const next = normalizePlan(body.plan);
        setPlan(next);
        if (!dryRun) {
          setRow(normalizeRow(body.row));
          setNote(typeof body.note === "string" ? body.note : "");
          // The row is written; the running engine is still serving the set it
          // discovered. The page's reload bar is what says so.
          onInstalled();
        }
      } catch {
        setError(BRIDGE_UNREACHABLE);
        if (dryRun) setPlan(null);
        else setAttempted(true);
      } finally {
        setBusy(null);
      }
    },
    [name, version, index, usable, plan, acceptedSha, onAct, onInstalled]
  );

  async function copySha(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedSha(value);
    } catch {
      // A browser that refuses the clipboard is not worth a banner; the first
      // twelve characters are on screen and can be selected by hand.
      setCopiedSha(null);
    }
  }

  const pill = plan ? verdictPill(plan.verdict) : null;
  const installable = plan !== null && (plan.verdict === "safe" || plan.verdict === "review");
  const accepted = plan !== null && acceptedSha === plan.sha256;

  return (
    <div
      data-testid="plugins-install-card"
      className="flex min-w-0 max-w-3xl flex-col gap-3 rounded-lg border border-border bg-card p-3"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Download aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        <span className="text-sm font-medium text-foreground">Install from the registry</span>
      </div>

      <p className="text-xs text-muted-foreground">
        The registry is a signed static index, not a service: the engine verifies its signature
        against a key pinned on this box, downloads the wheel the index names, checks its hash, and
        reads its source before anything is installed. Preview shows you all of that first.
      </p>

      <div className="flex min-w-0 flex-wrap items-end gap-2">
        <label className="flex min-w-0 flex-1 basis-48 flex-col gap-1 text-[11px] text-muted-foreground">
          Distribution name
          <input
            data-testid="plugins-install-name"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              changed();
            }}
            placeholder="genus-hostinfo"
            className="min-w-0 rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
          />
        </label>
        <label className="flex min-w-0 basis-28 flex-col gap-1 text-[11px] text-muted-foreground">
          Version (optional)
          <input
            data-testid="plugins-install-version"
            value={version}
            onChange={(event) => {
              setVersion(event.target.value);
              changed();
            }}
            placeholder="latest"
            className="min-w-0 rounded border border-border bg-background px-2 py-1 font-mono text-xs text-foreground"
          />
        </label>
        {/*
          Only when there is a choice to make. One configured index is not a
          decision, and a select with a single option is a control that asks a
          question with one answer.
        */}
        {usable.length > 1 ? (
          <label className="flex min-w-0 basis-full flex-col gap-1 text-[11px] text-muted-foreground sm:basis-56">
            Index
            <select
              data-testid="plugins-install-index"
              value={index || usable[0]}
              onChange={(event) => {
                setIndex(event.target.value);
                changed();
              }}
              className="min-w-0 rounded border border-border bg-background px-2 py-1 text-xs text-foreground"
            >
              {usable.map((url) => (
                <option key={url} value={url}>
                  {url}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <Button
          size="sm"
          variant="outline"
          data-testid="plugins-install-preview"
          disabled={!name.trim() || busy !== null}
          onClick={() => void post(true)}
        >
          {busy === "preview" ? "Reading the index…" : "Preview"}
        </Button>
      </div>

      <p
        data-testid="plugins-install-wheel-hint"
        className="break-words text-[11px] text-muted-foreground"
      >
        This takes a distribution name, never a path or a URL — a dashboard that could name either
        would be reading files or fetching on a caller&apos;s say-so. To install a local wheel, use{" "}
        <code className="break-all font-mono">genus plugin install ./x.whl --sha256 …</code> on the
        box.
      </p>

      {refused.length ? (
        <p
          data-testid="plugins-install-unusable-index"
          className="break-words text-[11px] text-warning"
        >
          Not offered as a choice, because the engine refuses a plugin index that is not{" "}
          <code className="font-mono">https</code> — its signature is the only thing standing
          between this box and whatever a mirror serves:{" "}
          <span className="break-all font-mono">{refused.join(", ")}</span>. Fix{" "}
          <code className="font-mono">ROBOTHOR_PLUGIN_INDEXES</code> on the box.
        </p>
      ) : null}

      {error ? (
        <p
          data-testid="plugins-install-error"
          className="break-words rounded-lg border border-destructive/30 bg-destructive/10 p-2 text-[11px] text-destructive"
        >
          {error}
        </p>
      ) : null}

      {plan && pill ? (
        <div
          data-testid="plugins-install-plan"
          className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-muted/40 p-3"
        >
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="min-w-0 break-all text-sm font-medium text-foreground">
              {plan.name}
            </span>
            <span className="shrink-0 rounded-full border border-border bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
              {plan.version || "version unknown"}
            </span>
            <span
              data-testid="plugins-install-verdict"
              data-verdict={plan.verdict}
              className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${pill.className}`}
            >
              {pill.label}
            </span>
          </div>

          {plan.summary ? (
            <p className="break-words text-[11px] text-muted-foreground">{plan.summary}</p>
          ) : null}

          <dl className="flex min-w-0 flex-col gap-1 text-[11px] text-muted-foreground">
            <div className="flex min-w-0 flex-wrap items-center gap-1">
              <dt className="shrink-0">Artifact</dt>
              <dd className="min-w-0 break-all font-mono text-foreground">{plan.filename}</dd>
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-1">
              <dt className="shrink-0">sha256</dt>
              {/*
                Twelve characters, because that is what a person can actually
                compare against what a publisher posted. The full hash is in the
                clipboard, where a machine comparison can have it.
              */}
              <dd className="min-w-0 break-all font-mono text-foreground">
                {plan.sha256.slice(0, 12)}…
              </dd>
              <button
                type="button"
                data-testid="plugins-install-copy-sha"
                onClick={() => void copySha(plan.sha256)}
                className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground"
              >
                <Copy aria-hidden className="size-3" />
                {/*
                  Keyed on the HASH, not on a boolean. The clipboard holds one
                  specific hash, and on the one control whose purpose is
                  comparing hashes, "copied" beside a different one is worse
                  than no affordance at all.
                */}
                {copiedSha === plan.sha256 ? "copied" : "copy"}
              </button>
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-1">
              <dt className="shrink-0">Signed by</dt>
              <dd className="min-w-0 break-all font-mono text-foreground">
                {plan.publisherKeyId || "no key recorded"}
              </dd>
            </div>
            {/*
              WHICH index answered. The picker names one and the engine is free
              to search the others when none was posted, so this is the field
              that makes the provenance claim checkable rather than assumed.
            */}
            <div className="flex min-w-0 flex-wrap items-center gap-1">
              <dt className="shrink-0">From</dt>
              <dd className="min-w-0 break-all font-mono text-foreground">
                {plan.indexUrl || "a wheel, not an index"}
              </dd>
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-1">
              <dt className="shrink-0">Contributes to</dt>
              <dd className="min-w-0 break-all font-mono text-foreground">
                {plan.groups.join(", ") || "nothing declared"}
              </dd>
            </div>
            {plan.membersAccounted ? (
              <div className="flex min-w-0 flex-wrap items-center gap-1">
                <dt className="shrink-0">Scanned</dt>
                <dd className="min-w-0 break-words">
                  {plan.filesScanned} source {plan.filesScanned === 1 ? "file" : "files"},{" "}
                  {plan.membersAccounted} wheel{" "}
                  {plan.membersAccounted === 1 ? "member" : "members"} accounted for
                </dd>
              </div>
            ) : null}
          </dl>

          {/*
            Every reason, in full, in the scanner's own words. They name a file
            and a line inside the wheel, which is what makes the verdict
            checkable rather than a label — and truncating the list would hide
            the one finding that mattered behind "and 4 more".
          */}
          {plan.reasons.length ? (
            <ul
              data-testid="plugins-install-reasons"
              className="flex min-w-0 list-disc flex-col gap-0.5 pl-4 text-[11px] text-muted-foreground"
            >
              {plan.reasons.map((reason) => (
                <li key={reason} className="break-words">
                  {reason}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[11px] text-muted-foreground">The scan found nothing to report.</p>
          )}

          {plan.verdict === "blocked" ? (
            <p className="break-words text-[11px] text-destructive">
              This will not be installed. A blocked verdict is not a warning to accept — the
              findings above are things this installer refuses outright, and there is no button
              here that overrides them.
            </p>
          ) : null}

          {plan.verdict === "review" ? (
            <label className="flex min-w-0 items-start gap-2 text-[11px] text-muted-foreground">
              <input
                type="checkbox"
                data-testid="plugins-install-accept"
                checked={accepted}
                onChange={(event) => setAcceptedSha(event.target.checked ? plan.sha256 : null)}
                className="mt-0.5 shrink-0"
              />
              <span className="min-w-0 break-words">
                I accept the review findings. Most plugins land here — anything that imports{" "}
                <code className="font-mono">os</code>, or whose manifest omits{" "}
                <code className="font-mono">entry_points:</code> — so this is the ordinary path, and
                the point of it is that somebody read the list above.
              </span>
            </label>
          ) : null}

          {installable ? (
            <div>
              <Button
                size="sm"
                data-testid="plugins-install-submit"
                /*
                  `attempted` is the terminal state, and it is set on EVERY
                  answer rather than only on a recorded row. After a 200 the
                  button used to revert from "Installing…" to "Install" with the
                  plan and the acceptance both still set, so a second press ran
                  pip a second time — and the only sign the first had worked was
                  one line of text below the fold on a phone. A 504 is the worse
                  half: it means the operation may still be running, so a retry
                  is the exact thing not to offer. Preview again to try again.
                */
                disabled={busy !== null || attempted || (plan.verdict === "review" && !accepted)}
                onClick={() => void post(false)}
              >
                {busy === "install" ? "Installing…" : "Install"}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      {/*
        `row` OR `note`: the route answers 200 with a null row when the install
        succeeded and the lockfile row could not be written, and says so in
        `note`. Gating on the row alone made that outcome silent.
      */}
      {row || (note && attempted) ? (
        <p
          data-testid="plugins-install-result"
          className="break-words rounded-lg border border-border bg-card p-2 text-[11px] text-muted-foreground"
        >
          {row ? (
            <>
              Recorded <span className="font-medium text-foreground">{row.name}</span> {row.version}{" "}
              in the lockfile as <span className="font-mono">{row.verdict}</span>
              {row.kinds.length ? `, contributing ${row.kinds.join(", ")}` : ""}
              {row.membersAccounted ? ` · ${row.membersAccounted} wheel members accounted for` : ""}
              .{" "}
            </>
          ) : (
            <>Nothing was recorded in the lockfile. </>
          )}
          Installing is not loading — the engine is still running the set it discovered.
          {/*
            The route's own `note`, which is non-empty exactly when the outcome
            was not clean — the distribution went in but its row could not be
            recorded, say. Dropping it makes a partial install read as a
            complete one, which is the one thing a governance record must not do.
          */}
          {note ? <span className="block pt-1 text-warning">{note}</span> : null}
        </p>
      ) : null}
    </div>
  );
}

export default PluginInstallCard;
