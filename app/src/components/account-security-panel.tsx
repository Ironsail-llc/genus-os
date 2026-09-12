"use client";

/**
 * /account/security — enrol a second factor, and change the local password.
 *
 * Every call goes through the BFF proxy (`/api/bridge/...`), which attaches
 * the session's bridge bearer server-side; no credential is ever held in this
 * component's props or in the URL.
 *
 * The provisioning secret is shown as selectable text plus the `otpauth://`
 * URI rather than a QR code: the app has no QR dependency, and adding one to
 * the browser bundle for a page an operator visits once is a poor trade. Every
 * authenticator app accepts a typed base32 secret.
 */

import { useCallback, useEffect, useState } from "react";

const MIN_PASSWORD_LENGTH = 12;

type MeResponse = {
  email?: string;
  role?: string;
  mfa_enabled?: boolean;
  mfa_setup_required?: boolean;
};

type Enrollment = { secret: string; otpauth_uri: string };

async function postJson(path: string, body: unknown): Promise<Response | null> {
  try {
    return await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return null;
  }
}

export function AccountSecurityPanel() {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [enrollPassword, setEnrollPassword] = useState("");
  const [code, setCode] = useState("");
  const [mfaError, setMfaError] = useState<string | null>(null);
  const [mfaDone, setMfaDone] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordDone, setPasswordDone] = useState(false);

  const loadMe = useCallback(async () => {
    try {
      const res = await fetch("/api/bridge/api/auth/me");
      if (!res.ok) return null;
      return (await res.json()) as MeResponse;
    } catch {
      // Leave the panel in its "unknown" state rather than asserting a status.
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const loaded = await loadMe();
      if (!cancelled && loaded) setMe(loaded);
    })();
    return () => {
      cancelled = true;
    };
  }, [loadMe]);

  const refreshMe = useCallback(async () => {
    const loaded = await loadMe();
    if (loaded) setMe(loaded);
  }, [loadMe]);

  async function enroll() {
    setMfaError(null);
    // The bridge requires the account password here: binding a second factor
    // is a change of authority, and a stolen session alone must not be able to
    // perform it. Checked locally first only so an empty box does not spend
    // one of that route's five attempts per minute.
    if (!enrollPassword) {
      setMfaError("Enter your current password to enable two-factor.");
      return;
    }
    const res = await postJson("/api/bridge/api/auth/mfa/enroll", { password: enrollPassword });
    if (!res?.ok) {
      setMfaError(
        res?.status === 401
          ? "That password is not correct."
          : "Could not start enrollment. Try again.",
      );
      return;
    }
    setEnrollment((await res.json()) as Enrollment);
    // It has done its job; do not leave it sitting in the React tree.
    setEnrollPassword("");
  }

  async function confirm() {
    setMfaError(null);
    const res = await postJson("/api/bridge/api/auth/mfa/confirm", { code: code.trim() });
    if (!res?.ok) {
      setMfaError("That code was not accepted. Check your authenticator and try again.");
      return;
    }
    // The secret has done its job; drop it from component state so it is not
    // sitting in a React tree (or a devtools snapshot) any longer than needed.
    setEnrollment(null);
    setCode("");
    setMfaDone(true);
    void refreshMe();
  }

  async function changePassword() {
    setPasswordError(null);
    setPasswordDone(false);
    if (newPassword.length < MIN_PASSWORD_LENGTH) {
      // Checked here purely so the operator is told before a round trip; the
      // bridge enforces the same minimum and is the authority.
      setPasswordError(`New password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    // A server-only route, not the generic bridge proxy: it holds this
    // session's refresh token, which is what lets the bridge revoke every
    // OTHER session without ejecting the operator from this very panel.
    const res = await postJson("/api/account/password", {
      current_password: currentPassword,
      new_password: newPassword,
    });
    if (!res?.ok) {
      setPasswordError("That current password is not correct.");
      return;
    }
    setCurrentPassword("");
    setNewPassword("");
    setPasswordDone(true);
  }

  const field =
    "w-full rounded-md border border-border bg-background px-3 py-2 text-sm " +
    "focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2";
  const button =
    "rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground " +
    "transition-[filter] hover:brightness-110 focus-visible:outline-2 focus-visible:outline-ring";

  const mfaOn = mfaDone || me?.mfa_enabled === true;

  return (
    <div className="flex w-full max-w-xl flex-col gap-8">
      <section className="flex flex-col gap-3">
        <h2 className="text-base font-semibold">Two-factor authentication</h2>

        {me?.mfa_setup_required && !mfaOn && (
          <p className="text-sm text-amber-700 dark:text-amber-300">
            Owner accounts must enable two-factor authentication.
          </p>
        )}

        {mfaOn ? (
          <p className="text-sm text-muted-foreground">
            Two-factor is on for this account. Use <code>genus user mfa-reset</code> on the host if
            you lose your authenticator.
          </p>
        ) : enrollment ? (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-muted-foreground">
              Add this secret to your authenticator app, then enter the six-digit code it shows.
            </p>
            <div className="flex flex-col gap-1">
              <span className="text-xs font-medium text-muted-foreground">Secret</span>
              <code className="select-all break-all rounded-md border border-border bg-muted px-3 py-2 font-mono text-sm">
                {enrollment.secret}
              </code>
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-xs font-medium text-muted-foreground">Setup URI</span>
              <code className="select-all break-all rounded-md border border-border bg-muted px-3 py-2 font-mono text-xs">
                {enrollment.otpauth_uri}
              </code>
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="mfa-code" className="text-xs font-medium text-muted-foreground">
                One-time code
              </label>
              <input
                id="mfa-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={8}
                className={field}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </div>
            <button type="button" className={`${button} self-start`} onClick={confirm}>
              Confirm
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="enroll-password"
                className="text-xs font-medium text-muted-foreground"
              >
                Confirm your password
              </label>
              <input
                id="enroll-password"
                type="password"
                autoComplete="current-password"
                className={field}
                value={enrollPassword}
                onChange={(e) => setEnrollPassword(e.target.value)}
              />
            </div>
            <button type="button" className={`${button} self-start`} onClick={enroll}>
              Enable two-factor
            </button>
          </div>
        )}

        {mfaError && (
          <p role="alert" className="text-sm text-destructive">
            {mfaError}
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-base font-semibold">Change password</h2>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="current-password" className="text-xs font-medium text-muted-foreground">
            Current password
          </label>
          <input
            id="current-password"
            type="password"
            autoComplete="current-password"
            className={field}
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="new-password" className="text-xs font-medium text-muted-foreground">
            New password
          </label>
          <input
            id="new-password"
            type="password"
            autoComplete="new-password"
            className={field}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            At least {MIN_PASSWORD_LENGTH} characters.
          </p>
        </div>
        <button type="button" className={`${button} self-start`} onClick={changePassword}>
          Change password
        </button>
        {passwordError && (
          <p role="alert" className="text-sm text-destructive">
            {passwordError}
          </p>
        )}
        {passwordDone && <p className="text-sm text-muted-foreground">Password changed.</p>}
      </section>
    </div>
  );
}
