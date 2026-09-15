"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, RefreshCw, ShieldCheck } from "lucide-react";

import { PageHeader } from "@/components/business/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ChannelAccess } from "@/components/views/settings/channel-access";
import { readBridgeReply } from "@/lib/bridge/read-reply";

/**
 * Channels: what can deliver, whether it is set up, and who may use it.
 *
 * Everything on this page is the bridge's own answer, read off
 * `crm/bridge/routers/channel_access.py` and `robothor/engine/admin_channels.py`.
 * Three of those answers are three-valued on purpose, and the whole screen is
 * built around not flattening them:
 *
 * * `configured` is `true | false | null`, and `null` means the channel's
 *   health report said nothing about configuration — NOT that it is
 *   unconfigured. It is painted as unknown, never green and never as "not set
 *   up", because an operator who reads "not configured" goes and configures a
 *   channel that may already work.
 * * `pending_pairings` is `int | null`, and `null` means the rows could not be
 *   read. "Nobody is waiting to be let in" is a claim, and making it because
 *   the database was down is the class of lie this codebase has shipped before.
 * * a verify that raised or hung comes back with `error_class` set and
 *   `configured: null` — and, being a real channel object in another process,
 *   it may come back with steps attached. Those steps are NOT rendered as
 *   passes. A verdict of "unknown" suppresses the step list entirely, because
 *   a green tick beside "could not verify" is exactly the reassurance nobody
 *   should be given.
 *
 * There is no field here that takes a credential, and there is no route behind
 * one: a channel's credentials are written with `genus channel add` on the box.
 * The page says that once and never offers a token input.
 *
 * It polls the listing every 60 s **while visible** — that listing is where the
 * pending counts come from, and the whole set costs one round trip.
 */

const BRIDGE = "/api/bridge";
const POLL_MS = 60_000;

export interface ChannelHealth {
  [key: string]: unknown;
}

export interface Channel {
  name: string;
  builtin: boolean;
  /** `null` is UNKNOWN. See the module docstring. */
  configured: boolean | null;
  health: ChannelHealth | null;
  verify_available: boolean;
  access_mode: string | null;
  /** `null` is "could not be read", never zero. */
  pending_pairings: number | null;
}

export interface VerifyStep {
  step: string;
  ok: boolean;
  detail?: string | null;
}

export interface VerifyResult {
  channel: string;
  configured: boolean | null;
  verify_available: boolean;
  steps: VerifyStep[];
  error_class?: string | null;
}

type Verdict = "passed" | "failed" | "unconfigured" | "unknown";

interface Reading {
  verdict: Verdict;
  summary: string;
  /** Steps are shown only for a verdict that earned them. */
  steps: VerifyStep[];
}

/**
 * What a verify reply actually supports saying.
 *
 * The order of the tests is the point. `error_class` and an unknown
 * `configured` are checked BEFORE the steps, so a channel that timed out
 * cannot be rendered from whatever its steps array happened to contain — the
 * engine's own contract is that a raise or a hang answers with one failed
 * step, but a page that trusts an array it was handed by a separately
 * versioned process is one release away from painting a tick over an outage.
 */
export function readVerify(result: VerifyResult): Reading {
  const steps = Array.isArray(result.steps) ? result.steps : [];

  if (result.error_class) {
    return {
      verdict: "unknown",
      summary: `Could not verify — ${result.error_class}. The channel raised or did not answer, so nothing about it has been proven.`,
      steps: [],
    };
  }
  if (!result.verify_available) {
    return {
      verdict: "unknown",
      summary:
        "This channel cannot prove anything about itself — it declares no verification, so nothing was tried. Its state above comes from its own health report.",
      steps: [],
    };
  }
  if (result.configured === null || result.configured === undefined) {
    return {
      verdict: "unknown",
      summary:
        "Could not verify — the channel did not say whether it is configured, so nothing about it has been proven.",
      steps: [],
    };
  }
  if (result.configured === false) {
    const detail = steps.find((step) => step.detail)?.detail;
    return {
      verdict: "unconfigured",
      summary: `This channel has never been set up on this instance, so there was nothing to try${
        detail ? ` — ${detail}` : ""
      }.`,
      steps: [],
    };
  }
  if (steps.length === 0) {
    return {
      verdict: "unknown",
      summary: "The channel answered with no checks at all, so nothing about it has been proven.",
      steps: [],
    };
  }
  const bad = steps.filter((step) => !step.ok).length;
  if (bad === 0) {
    return {
      verdict: "passed",
      summary: `Verified — all ${steps.length} ${steps.length === 1 ? "check" : "checks"} passed.`,
      steps,
    };
  }
  return {
    verdict: "failed",
    summary: `${bad} of ${steps.length} ${steps.length === 1 ? "check" : "checks"} did not pass.`,
    steps,
  };
}

const VERDICT_CLASS: Record<Verdict, string> = {
  passed: "text-success",
  failed: "text-destructive",
  unconfigured: "text-muted-foreground",
  unknown: "text-warning",
};

/** A health value as the API sent it — this page redacts nothing and adds nothing. */
function healthValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function configuredLabel(configured: boolean | null): string {
  if (configured === true) return "Configured";
  if (configured === false) return "Not configured";
  return "Unknown";
}

function configuredClass(configured: boolean | null): string {
  if (configured === true) return "border-success/30 bg-success/10 text-success";
  if (configured === false) return "border-border bg-muted text-muted-foreground";
  return "border-warning/30 bg-warning/10 text-warning";
}

export interface ChannelsPageProps {
  /** The Settings container unmounts an inactive page; this is the poll gate. */
  visible?: boolean;
}

export function ChannelsPage({ visible = true }: ChannelsPageProps) {
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [verifying, setVerifying] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, VerifyResult>>({});
  const [verifyErrors, setVerifyErrors] = useState<Record<string, string>>({});
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});

  // Re-created on every render-relevant change; the poll must not be, or the
  // interval would be torn down and rebuilt on each tick.
  const loadRef = useRef<() => Promise<void>>(async () => {});

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${BRIDGE}/api/channels`);
      if (!res.ok) {
        setListError(await readBridgeReply(res));
        return;
      }
      const body = (await res.json()) as { channels?: Channel[] };
      setChannels(Array.isArray(body.channels) ? body.channels : []);
      setListError(null);
    } catch {
      setListError("The dashboard could not reach the bridge. Check that the service is running.");
    } finally {
      setLoading(false);
    }
  }, []);

  loadRef.current = load;

  useEffect(() => {
    if (!visible) return;
    void loadRef.current();
    const timer = setInterval(() => void loadRef.current(), POLL_MS);
    return () => clearInterval(timer);
  }, [visible]);

  const setVerifyError = (name: string, message: string | null) =>
    setVerifyErrors((prev) => {
      const next = { ...prev };
      if (message) next[name] = message;
      else delete next[name];
      return next;
    });

  async function runVerify(channel: Channel) {
    setVerifying(channel.name);
    setVerifyError(channel.name, null);
    const target = (targets[channel.name] ?? "").trim();
    try {
      const res = await fetch(`${BRIDGE}/api/channels/${channel.name}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(target ? { target } : {}),
      });
      if (!res.ok) {
        setVerifyError(channel.name, await readBridgeReply(res));
        return;
      }
      const result = (await res.json()) as VerifyResult;
      setResults((prev) => ({ ...prev, [channel.name]: result }));
    } catch {
      setVerifyError(
        channel.name,
        "The dashboard could not reach the bridge to verify that channel."
      );
    } finally {
      setVerifying(null);
    }
  }

  const rows = channels ?? [];

  return (
    <div className="flex flex-col gap-4 p-4" data-testid="settings-page-channels">
      <PageHeader title="Channels" description="What can deliver, and who may use it.">
        <Button
          variant="outline"
          size="sm"
          onClick={() => void loadRef.current()}
          data-testid="channels-refresh"
        >
          <RefreshCw aria-hidden />
          Refresh
        </Button>
      </PageHeader>

      <p
        className="max-w-3xl text-xs text-muted-foreground"
        data-testid="channels-credential-hint"
      >
        A channel&apos;s credentials are added on the box with{" "}
        <span className="font-mono">genus channel add</span> and are never typed into the Helm — this
        page reads what each channel says about itself, proves it on demand, and settles who may
        reach the instance over it.
      </p>

      <div className="flex flex-col gap-3" data-testid="channels-page">
        {loading && !channels ? (
          <div
            className="flex items-center gap-2 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground"
            data-testid="channels-loading"
          >
            <Loader2 aria-hidden className="size-4 animate-spin" />
            Reading the channel state from the bridge…
          </div>
        ) : null}

        {listError ? (
          <div
            className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4"
            data-testid="channels-error"
          >
            <p className="text-sm font-medium text-destructive">The channel listing failed</p>
            <p className="text-xs text-muted-foreground">{listError}</p>
            {rows.length > 0 ? (
              <p className="text-xs text-muted-foreground">
                The cards below are what the bridge last answered with, and may be out of date.
              </p>
            ) : null}
            <div>
              <Button variant="outline" size="sm" onClick={() => void loadRef.current()}>
                Try again
              </Button>
            </div>
          </div>
        ) : null}

        {!listError && !loading && rows.length === 0 ? (
          <div
            className="rounded-lg border border-dashed border-border bg-card/40 p-4"
            data-testid="channels-empty"
          >
            <p className="text-sm font-medium text-foreground">This engine registers no channels</p>
            <p className="mt-1 max-w-xl text-xs text-muted-foreground">
              Nothing can deliver an agent&apos;s answer anywhere, and nothing can reach the
              instance from outside. Check that the engine is running the same version as the
              dashboard.
            </p>
          </div>
        ) : null}

        {rows.map((channel) => {
          const result = results[channel.name];
          const reading = result ? readVerify(result) : null;
          const health = channel.health && typeof channel.health === "object" ? channel.health : {};
          const healthEntries = Object.entries(health).filter(([key]) => key !== "channel");
          const isOpen = Boolean(open[channel.name]);

          return (
            <section
              key={channel.name}
              data-testid={`channel-card-${channel.name}`}
              className="flex flex-col gap-3 rounded-lg border border-border bg-card p-3.5"
            >
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-medium text-foreground">{channel.name}</h3>
                <Badge
                  variant="outline"
                  className="text-muted-foreground"
                  data-testid={`channel-builtin-${channel.name}`}
                >
                  {channel.builtin ? "built-in" : "plugin"}
                </Badge>
                <Badge
                  variant="outline"
                  className={configuredClass(channel.configured)}
                  data-testid={`channel-configured-${channel.name}`}
                >
                  {configuredLabel(channel.configured)}
                </Badge>
              </div>

              <dl className="grid grid-cols-1 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-2">
                <div className="flex flex-wrap items-baseline gap-1.5">
                  <dt className="text-muted-foreground">Access mode</dt>
                  <dd className="text-foreground" data-testid={`channel-access-mode-${channel.name}`}>
                    {channel.access_mode ?? "unknown — the setting could not be resolved"}
                  </dd>
                </div>
                <div className="flex flex-wrap items-baseline gap-1.5">
                  <dt className="text-muted-foreground">Waiting to pair</dt>
                  <dd
                    className={
                      channel.pending_pairings === null ? "text-warning" : "text-foreground"
                    }
                    data-testid={`channel-pending-count-${channel.name}`}
                  >
                    {channel.pending_pairings === null
                      ? "could not be read"
                      : `${channel.pending_pairings} ${
                          channel.pending_pairings === 1 ? "sender" : "senders"
                        }`}
                  </dd>
                </div>
              </dl>

              <div
                className="flex flex-col gap-0.5 rounded-md border border-border bg-background/60 p-2"
                data-testid={`channel-health-${channel.name}`}
              >
                <span className="text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                  Health report
                </span>
                {healthEntries.length === 0 ? (
                  <span className="text-xs text-muted-foreground">
                    The channel reported nothing about itself.
                  </span>
                ) : (
                  healthEntries.map(([key, value]) => (
                    <span key={key} className="break-all font-mono text-[11px] text-foreground">
                      {key}: {healthValue(value)}
                    </span>
                  ))
                )}
              </div>

              <div className="flex flex-col gap-2">
                {channel.verify_available ? (
                  <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={verifying === channel.name}
                      data-testid={`channel-verify-${channel.name}`}
                      onClick={() => void runVerify(channel)}
                    >
                      {verifying === channel.name ? (
                        <Loader2 aria-hidden className="animate-spin" />
                      ) : (
                        <ShieldCheck aria-hidden />
                      )}
                      Verify
                    </Button>
                    <label className="flex flex-1 items-center gap-2 text-[11px] uppercase tracking-[0.08em] text-muted-foreground sm:max-w-sm">
                      Target
                      <Input
                        data-testid={`channel-verify-target-${channel.name}`}
                        autoComplete="off"
                        spellCheck={false}
                        placeholder="optional — the channel's own default"
                        className="w-full"
                        value={targets[channel.name] ?? ""}
                        onChange={(event) =>
                          setTargets((prev) => ({ ...prev, [channel.name]: event.target.value }))
                        }
                      />
                    </label>
                  </div>
                ) : (
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid={`channel-verify-unavailable-${channel.name}`}
                  >
                    This channel declares no verification, so there is nothing to prove on demand.
                  </p>
                )}

                {channel.verify_available ? (
                  <p className="text-[11px] text-muted-foreground">
                    Verifying may really send a message on this channel, and it is recorded in the
                    audit log.
                  </p>
                ) : null}

                {reading ? (
                  <p
                    aria-live="polite"
                    data-verdict={reading.verdict}
                    data-testid={`channel-verify-result-${channel.name}`}
                    className={`text-xs ${VERDICT_CLASS[reading.verdict]}`}
                  >
                    {reading.summary}
                  </p>
                ) : null}

                {reading && reading.steps.length > 0 ? (
                  <ul
                    className="flex flex-col gap-0.5"
                    data-testid={`channel-verify-steps-${channel.name}`}
                  >
                    {reading.steps.map((step) => (
                      <li
                        key={step.step}
                        data-ok={String(Boolean(step.ok))}
                        data-testid={`channel-verify-step-${channel.name}-${step.step}`}
                        className={`text-xs ${step.ok ? "text-success" : "text-destructive"}`}
                      >
                        <span aria-hidden>{step.ok ? "✓" : "✗"}</span>{" "}
                        <span className="font-mono">{step.step}</span>
                        {step.detail ? (
                          <span className="text-muted-foreground"> — {step.detail}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}

                {verifyErrors[channel.name] ? (
                  <p
                    className="text-xs text-destructive"
                    data-testid={`channel-verify-error-${channel.name}`}
                  >
                    {verifyErrors[channel.name]}
                  </p>
                ) : null}
              </div>

              <div className="flex flex-col gap-2">
                <div>
                  <Button
                    variant="ghost"
                    size="xs"
                    aria-expanded={isOpen}
                    data-testid={`channel-access-toggle-${channel.name}`}
                    onClick={() =>
                      setOpen((prev) => ({ ...prev, [channel.name]: !prev[channel.name] }))
                    }
                  >
                    {isOpen ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />}
                    Who may use this channel
                  </Button>
                </div>
                {isOpen ? (
                  <ChannelAccess channel={channel.name} onSettled={() => void loadRef.current()} />
                ) : null}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
