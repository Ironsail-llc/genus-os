"use client";

/**
 * The first-run stepper: `docker compose up` to a signed-in dashboard, without
 * a terminal.
 *
 * Six steps, and the order is the order in which things become possible. The
 * operator account has to exist before anything can be signed in; a provider
 * key has to work before a model can answer; a channel and an agent fleet are
 * optional; and the owner's second factor is last because enrolling one needs
 * the session step 2 produced.
 *
 * Three rules this component follows and the tests hold down:
 *
 * - **The claim token lives in React state only.** Not `localStorage`, not a
 *   cookie, not the URL after the first exchange — the setup token is stripped
 *   from the address bar as soon as it has been spent, so a screenshot, a
 *   shared link or a referrer header cannot carry it.
 * - **A key is never read back.** Every credential field is write-only: what
 *   the operator types goes into one POST and is then cleared from state.
 * - **A step that has not succeeded does not advance.** The provider step is
 *   gated on a real completion coming back `ok: true`; a failed test is shown,
 *   not swallowed, because "configured but not working" is the state this
 *   whole wizard exists to prevent.
 *
 * Every colour is a design token from `globals.css`; the layout works at
 * 375 px (one column, the stepper collapses to a row of dots).
 */

import { signIn } from "next-auth/react";
import { useCallback, useEffect, useState } from "react";

import { AccountSecurityPanel } from "@/components/account-security-panel";

const MIN_PASSWORD_LENGTH = 12;

/** The wizard's steps, in order. Index 0 is the welcome pane. */
export const STEPS = [
  "Welcome",
  "Operator",
  "Provider",
  "Channel",
  "Agents",
  "Two-factor",
  "Done",
] as const;

/** Steps that show the doctor strip: Provider onwards. */
const FIRST_STEP_WITH_DOCTOR = 2;

type DoctorRow = { id: string; status: string };

type Detected = {
  providers: { id: string; label?: string; configured: boolean }[];
  models: { id: string; provider: string }[];
  ollama: { reachable: boolean; tool_models: string[] };
  telegram: { configured: boolean };
  doctor: { checks: DoctorRow[] };
};

type Preset = { id: string; title: string; description: string };

const PRESETS: Preset[] = [
  {
    id: "minimal",
    title: "Minimal",
    description:
      "Chat, a concierge and a canary. Three agents, nothing scheduled.",
  },
  {
    id: "standard",
    title: "Standard",
    description:
      "Email triage, calendar watch and daily briefings. Ten agents.",
  },
  {
    id: "full",
    title: "Full",
    description: "Everything in the catalogue, including CRM, comms and ops.",
  },
];

const field =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm " +
  "focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2";
const primaryButton =
  "rounded-md bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground " +
  "transition-[filter] hover:brightness-110 focus-visible:outline-2 " +
  "focus-visible:outline-ring focus-visible:outline-offset-2 disabled:opacity-60";
const quietButton =
  "rounded-md border border-border px-5 py-2.5 text-sm font-medium text-foreground " +
  "hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring " +
  "focus-visible:outline-offset-2 disabled:opacity-60";

async function post<T>(
  path: string,
  claim: string,
  body: unknown,
): Promise<T | null> {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${claim}`,
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export function SetupWizard({ initialToken }: { initialToken: string }) {
  const [step, setStep] = useState(0);
  const [claim, setClaim] = useState<string | null>(null);
  const [claimError, setClaimError] = useState<string | null>(null);
  const [detected, setDetected] = useState<Detected | null>(null);

  // Step 2 — operator
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [operatorError, setOperatorError] = useState<string | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [busy, setBusy] = useState(false);

  // Step 3 — provider
  const [providerId, setProviderId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    latency_ms?: number;
    error_class?: string | null;
  } | null>(null);

  // Step 4 — channel
  const [botToken, setBotToken] = useState("");
  const [channelResult, setChannelResult] = useState<{
    ok: boolean;
    bot?: string;
  } | null>(null);

  // Step 5 — agents
  const [preset, setPreset] = useState("standard");
  const [installed, setInstalled] = useState<string[] | null>(null);

  // Step 7 — done
  const [next, setNext] = useState<string | null>(null);

  // Exchange the printed token for a claim, once, then clear it out of the
  // address bar so it cannot be screenshotted, shared or sent as a referrer.
  useEffect(() => {
    if (!initialToken || claim) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch("/api/setup/claim", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: initialToken }),
        });
        if (cancelled) return;
        if (!res.ok) {
          setClaimError(
            "That setup link is not valid any more. Run `genus auth setup-link` on the " +
              "server for a fresh one.",
          );
          return;
        }
        const payload = (await res.json()) as { claim_token?: string };
        if (payload.claim_token) setClaim(payload.claim_token);
      } catch {
        if (!cancelled)
          setClaimError("Could not reach the server. Is the bridge running?");
      } finally {
        if (typeof window !== "undefined" && window.history?.replaceState) {
          window.history.replaceState({}, "", "/setup");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [initialToken, claim]);

  const refreshDetected = useCallback(async () => {
    if (!claim) return;
    try {
      const res = await fetch("/api/setup/detect", {
        headers: { Authorization: `Bearer ${claim}` },
      });
      if (res.ok) setDetected((await res.json()) as Detected);
    } catch {
      // The strip is informational; an unreachable probe leaves it empty.
    }
  }, [claim]);

  useEffect(() => {
    void refreshDetected();
  }, [refreshDetected]);

  async function submitOperator() {
    setOperatorError(null);
    if (!name.trim() || !email.trim()) {
      setOperatorError("A name and an email address are required.");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      // Checked here only so the operator hears it before a round trip; the
      // bridge enforces the same minimum and is the authority.
      setOperatorError(`Use at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (password !== confirmPassword) {
      setOperatorError("Those two passwords do not match.");
      return;
    }
    if (!claim) return;

    setBusy(true);
    try {
      const created = await post<{ signed_in?: boolean }>(
        "/api/setup/operator",
        claim,
        {
          name: name.trim(),
          email: email.trim(),
          password,
        },
      );
      if (!created) {
        setOperatorError(
          "The server refused that. An operator account may already exist on this instance.",
        );
        return;
      }
      // The hand-off: the bridge has already minted a session, and this puts
      // the equivalent one in the browser through the SAME credentials path a
      // normal sign-in uses, so there is no second way to become signed in.
      // It can fail on a box whose dashboard booted before local login was
      // enabled — that is reported at the end rather than hidden.
      let ok = false;
      try {
        const result = (await signIn("local", {
          email: email.trim(),
          password,
          redirect: false,
        })) as { ok?: boolean; error?: string } | undefined;
        ok = Boolean(result?.ok && !result.error);
      } catch {
        ok = false;
      }
      setSignedIn(ok);
      // It has done its job. Do not leave it in the React tree.
      setPassword("");
      setConfirmPassword("");
      setStep(2);
      void refreshDetected();
    } finally {
      setBusy(false);
    }
  }

  async function testProvider() {
    if (!claim) return;
    setBusy(true);
    setTestResult(null);
    try {
      const result = await post<{
        ok: boolean;
        latency_ms?: number;
        error_class?: string | null;
      }>("/api/setup/provider", claim, {
        provider_id: providerId,
        api_key: apiKey,
        default_model: model,
      });
      setTestResult(result ?? { ok: false, error_class: "Unreachable" });
      if (result?.ok) setApiKey("");
      void refreshDetected();
    } finally {
      setBusy(false);
    }
  }

  async function verifyChannel() {
    if (!claim) return;
    setBusy(true);
    try {
      const result = await post<{ ok: boolean; bot?: string }>(
        "/api/setup/channel",
        claim,
        {
          telegram_bot_token: botToken,
        },
      );
      setChannelResult(result ?? { ok: false });
      if (result?.ok) setBotToken("");
    } finally {
      setBusy(false);
    }
  }

  async function installAgents() {
    if (!claim) return;
    setBusy(true);
    try {
      const result = await post<{ installed: string[] }>(
        "/api/setup/agent",
        claim,
        { preset },
      );
      setInstalled(result?.installed ?? []);
      setStep(5);
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    if (!claim) return;
    setBusy(true);
    try {
      const result = await post<{ next: string }>(
        "/api/setup/complete",
        claim,
        {},
      );
      setNext(result?.next ?? "/?v=chat");
      setStep(6);
    } finally {
      setBusy(false);
    }
  }

  const models = detected?.models ?? [];
  const modelsForProvider = providerId
    ? models.filter((entry) => entry.provider === providerId)
    : models;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col gap-6 px-4 py-8 sm:px-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-xl font-semibold text-foreground sm:text-2xl">
          Set up Genus OS
        </h1>
        <Stepper current={step} />
      </header>

      {claimError && (
        <p
          role="alert"
          className="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
        >
          {claimError}
        </p>
      )}

      {step >= FIRST_STEP_WITH_DOCTOR && step < 6 && (
        <DoctorStrip rows={detected?.doctor.checks ?? []} />
      )}

      <section className="rounded-lg border border-border bg-card p-4 sm:p-6">
        {step === 0 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">Welcome</h2>
            <p className="text-sm text-muted-foreground">
              This page is reachable only with the one-time link{" "}
              <code>genus init</code> printed, and never again once setup is
              finished — so finish it now, in this tab.
            </p>
            <div className="flex justify-end">
              <button
                type="button"
                className={primaryButton}
                disabled={!claim}
                onClick={() => setStep(1)}
              >
                Start
              </button>
            </div>
          </div>
        )}

        {step === 1 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">
              Your operator account
            </h2>
            <p className="text-sm text-muted-foreground">
              This becomes the owner of this instance. There is exactly one.
            </p>
            <Labelled id="setup-name" label="Full name">
              <input
                id="setup-name"
                className={field}
                autoComplete="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Labelled>
            <Labelled id="setup-email" label="Email">
              <input
                id="setup-email"
                type="email"
                className={field}
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Labelled>
            <Labelled id="setup-password" label="Password">
              <input
                id="setup-password"
                type="password"
                className={field}
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Labelled>
            <Labelled id="setup-password-confirm" label="Confirm password">
              <input
                id="setup-password-confirm"
                type="password"
                className={field}
                autoComplete="new-password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
            </Labelled>
            <p className="text-xs text-muted-foreground">
              At least {MIN_PASSWORD_LENGTH} characters.
            </p>
            {operatorError && (
              <p role="alert" className="text-sm text-destructive">
                {operatorError}
              </p>
            )}
            <div className="flex justify-end">
              <button
                type="button"
                className={primaryButton}
                disabled={busy || !claim}
                onClick={() => void submitOperator()}
              >
                {busy ? "Creating…" : "Create account"}
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">
              A model provider
            </h2>
            <p className="text-sm text-muted-foreground">
              One API key, tested for real before this step will let you past.
            </p>
            <Labelled id="setup-provider" label="Provider">
              <select
                id="setup-provider"
                className={field}
                value={providerId}
                onChange={(e) => {
                  setProviderId(e.target.value);
                  setTestResult(null);
                }}
              >
                <option value="">Choose a provider…</option>
                {(detected?.providers ?? []).map((provider) => (
                  <option key={provider.id} value={provider.id}>
                    {provider.label || provider.id}
                  </option>
                ))}
              </select>
            </Labelled>
            <Labelled id="setup-key" label="API key">
              <input
                id="setup-key"
                type="password"
                className={field}
                autoComplete="off"
                value={apiKey}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  setTestResult(null);
                }}
              />
            </Labelled>
            <p className="text-xs text-muted-foreground">
              Stored in the instance vault. It is never shown again.
            </p>
            <Labelled id="setup-model" label="Default model">
              {modelsForProvider.length > 0 ? (
                <select
                  id="setup-model"
                  className={field}
                  value={model}
                  onChange={(e) => {
                    setModel(e.target.value);
                    setTestResult(null);
                  }}
                >
                  <option value="">Choose a model…</option>
                  {modelsForProvider.map((entry) => (
                    <option key={entry.id} value={entry.id}>
                      {entry.id}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  id="setup-model"
                  className={field}
                  placeholder="openrouter/<provider>/<model>"
                  value={model}
                  onChange={(e) => {
                    setModel(e.target.value);
                    setTestResult(null);
                  }}
                />
              )}
            </Labelled>

            {testResult && (
              <p
                role="status"
                className={
                  testResult.ok
                    ? "text-sm text-success-foreground"
                    : "text-sm text-destructive"
                }
              >
                {testResult.ok
                  ? `The provider answered in ${testResult.latency_ms ?? 0}ms.`
                  : `That key did not work${
                      testResult.error_class
                        ? ` (${testResult.error_class})`
                        : ""
                    }. Check it and test again.`}
              </p>
            )}

            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className={quietButton}
                disabled={busy || !providerId || !apiKey || !model}
                onClick={() => void testProvider()}
              >
                {busy ? "Testing…" : "Test connection"}
              </button>
              <button
                type="button"
                className={primaryButton}
                disabled={!testResult?.ok}
                onClick={() => setStep(3)}
              >
                Next
              </button>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">
              A channel (optional)
            </h2>
            <p className="text-sm text-muted-foreground">
              Paste a Telegram bot token to reach this instance from your phone,
              or skip it.
            </p>
            <Labelled id="setup-bot-token" label="Telegram bot token">
              <input
                id="setup-bot-token"
                type="password"
                className={field}
                autoComplete="off"
                value={botToken}
                onChange={(e) => {
                  setBotToken(e.target.value);
                  setChannelResult(null);
                }}
              />
            </Labelled>
            {channelResult && (
              <p
                role="status"
                className={
                  channelResult.ok
                    ? "text-sm text-success-foreground"
                    : "text-sm text-destructive"
                }
              >
                {channelResult.ok
                  ? `Verified${channelResult.bot ? `: @${channelResult.bot}` : ""}.`
                  : "Telegram did not accept that token."}
              </p>
            )}
            <div className="flex flex-wrap justify-end gap-2">
              <button
                type="button"
                className={quietButton}
                onClick={() => setStep(4)}
              >
                Skip
              </button>
              <button
                type="button"
                className={quietButton}
                disabled={busy || !botToken}
                onClick={() => void verifyChannel()}
              >
                {busy ? "Verifying…" : "Verify"}
              </button>
              <button
                type="button"
                className={primaryButton}
                disabled={!channelResult?.ok}
                onClick={() => setStep(4)}
              >
                Next
              </button>
            </div>
          </div>
        )}

        {step === 4 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">
              Your first agents
            </h2>
            <p className="text-sm text-muted-foreground">
              Every one of these is a YAML manifest you can edit afterwards.
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              {PRESETS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  aria-pressed={preset === option.id}
                  onClick={() => setPreset(option.id)}
                  className={`flex flex-col gap-1 rounded-md border p-3 text-left focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2 ${
                    preset === option.id
                      ? "border-primary bg-accent"
                      : "border-border hover:bg-accent"
                  }`}
                >
                  <span className="text-sm font-medium text-foreground">
                    {option.title}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {option.description}
                  </span>
                </button>
              ))}
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                className={primaryButton}
                disabled={busy}
                onClick={() => void installAgents()}
              >
                {busy ? "Installing…" : "Install"}
              </button>
            </div>
          </div>
        )}

        {step === 5 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">
              Two-factor authentication
            </h2>
            {installed && installed.length > 0 && (
              <p className="text-sm text-muted-foreground">
                Installed {installed.length} agent
                {installed.length === 1 ? "" : "s"}.
              </p>
            )}
            <p className="text-sm text-muted-foreground">
              Required for the owner account: a password is now the whole of
              what protects this instance and every credential in it.
            </p>
            {signedIn ? (
              <AccountSecurityPanel />
            ) : (
              <p
                role="alert"
                className="rounded-md bg-warning/10 p-3 text-sm text-warning-foreground"
              >
                This browser is not signed in yet — the dashboard needs a
                restart to offer the password form. Finish below, restart it,
                then sign in and enrol a second factor from Account → Security.
              </p>
            )}
            <div className="flex justify-end">
              <button
                type="button"
                className={primaryButton}
                disabled={busy}
                onClick={() => void finish()}
              >
                {busy ? "Finishing…" : "Finish setup"}
              </button>
            </div>
          </div>
        )}

        {step === 6 && (
          <div className="flex flex-col gap-4">
            <h2 className="text-base font-medium text-foreground">Ready</h2>
            <p className="text-sm text-muted-foreground">
              Setup is closed: this page will not load again.
            </p>
            <div className="flex justify-end">
              <a
                className={primaryButton}
                href={signedIn ? (next ?? "/?v=chat") : "/signin"}
              >
                {signedIn ? "Open the dashboard" : "Go to sign in"}
              </a>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}

function Labelled({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      {children}
    </div>
  );
}

function Stepper({ current }: { current: number }) {
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      {STEPS.map((label, index) => (
        <li
          key={label}
          aria-current={index === current ? "step" : undefined}
          className={
            index === current
              ? "font-medium text-foreground"
              : index < current
                ? "text-primary"
                : ""
          }
        >
          <span className="hidden sm:inline">
            {index + 1}. {label}
          </span>
          <span aria-hidden className="sm:hidden">
            {index === current ? "●" : index < current ? "✓" : "○"}
          </span>
        </li>
      ))}
    </ol>
  );
}

/**
 * The doctor's required checks, as pass/fail pills.
 *
 * Ids and statuses, which is all the bridge sends: the full report names which
 * services run and which migrations are missing, and that is not something a
 * page reachable without a session should print.
 */
function DoctorStrip({ rows }: { rows: DoctorRow[] }) {
  if (rows.length === 0) return null;
  return (
    <ul aria-label="Instance checks" className="flex flex-wrap gap-1.5">
      {rows.map((row) => (
        <li
          key={row.id}
          className={`rounded-full px-2 py-0.5 text-xs ${
            row.status === "pass"
              ? "bg-success/15 text-success-foreground"
              : row.status === "fail"
                ? "bg-destructive/15 text-destructive"
                : "bg-muted text-muted-foreground"
          }`}
        >
          {row.id}
        </li>
      ))}
    </ul>
  );
}
