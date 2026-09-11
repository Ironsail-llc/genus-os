"use client";

/**
 * Email + password sign-in form.
 *
 * A client component rather than a server action on purpose. The second step
 * (a one-time code) has to appear WITHOUT losing what was already typed, and
 * the alternatives both cost something real: a server action would have to
 * round-trip the email and password through a redirect — i.e. through the URL
 * bar, the browser history and every access log on the way — or make the
 * operator retype their password to answer the code prompt.
 *
 * `signIn(..., { redirect: false })` keeps the credentials in one POST to the
 * Auth.js endpoint and hands back the failure code, which is the only thing
 * this component is allowed to distinguish: `mfa_required` (reveal the code
 * field) or everything else (one generic message). That mirrors the bridge's
 * own contract exactly — see robothor/auth/local_login.py.
 */

import { signIn } from "next-auth/react";
import { useState } from "react";

const GENERIC_FAILURE = "That email or password is not correct.";

/**
 * `callbackUrl` reaches this component from the query string, so
 * `/signin?callbackUrl=https://evil.example.com` is attacker-controlled.
 * Navigating there after a successful sign-in would be an open redirect on the
 * one page where the victim has just typed their password.
 *
 * String prefix checks are not enough, and the first version proved it: the
 * WHATWG URL parser STRIPS tab, LF and CR anywhere in a URL before parsing, so
 * `/\t/evil.example.com` is `//evil.example.com` — a protocol-relative URL to
 * another host — while `startsWith("//")` sees a harmless-looking path.
 * Backslash is normalised to `/` for the same reason.
 *
 * So the value is resolved against a sentinel origin with the SAME parser the
 * browser will use, and is accepted only if it stayed there. Control
 * characters are rejected outright rather than reasoned about.
 */
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/;
const SENTINEL_ORIGIN = "https://x.invalid";

export function safeCallbackUrl(candidate: string | undefined): string {
  const value = (candidate ?? "").trim();
  if (!value.startsWith("/")) return "/";
  if (value.includes("\\") || CONTROL_CHARACTERS.test(value)) return "/";
  try {
    const resolved = new URL(value, SENTINEL_ORIGIN);
    if (resolved.origin !== SENTINEL_ORIGIN) return "/";
    return `${resolved.pathname}${resolved.search}${resolved.hash}`;
  } catch {
    return "/";
  }
}

export function LocalSignInForm({ callbackUrl }: { callbackUrl: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const destination = safeCallbackUrl(callbackUrl);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!email.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      const result = (await signIn("local", {
        email: email.trim(),
        password,
        code,
        redirect: false,
        redirectTo: destination,
      })) as { ok?: boolean; code?: string; error?: string } | undefined;

      if (result?.ok && !result.error) {
        window.location.assign(destination);
        return;
      }
      if (result?.code === "mfa_required") {
        setNeedsCode(true);
        setCode("");
        setError(null);
        return;
      }
      setError(GENERIC_FAILURE);
    } catch {
      setError(GENERIC_FAILURE);
    } finally {
      setBusy(false);
    }
  }

  const field =
    "w-full rounded-md border border-border bg-background px-3 py-2 text-sm " +
    "focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2";

  return (
    <form onSubmit={submit} className="flex w-full flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <label htmlFor="local-email" className="text-xs font-medium text-muted-foreground">
          Email
        </label>
        <input
          id="local-email"
          name="email"
          type="email"
          autoComplete="username"
          className={field}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="local-password" className="text-xs font-medium text-muted-foreground">
          Password
        </label>
        <input
          id="local-password"
          name="password"
          type="password"
          autoComplete="current-password"
          className={field}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>

      {needsCode && (
        <div className="flex flex-col gap-1.5">
          <label htmlFor="local-code" className="text-xs font-medium text-muted-foreground">
            One-time code
          </label>
          <input
            id="local-code"
            name="code"
            type="text"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={8}
            className={field}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Enter the six-digit code from your authenticator app.
          </p>
        </div>
      )}

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={busy}
        className="w-full rounded-md bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-[filter] hover:brightness-110 focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2 disabled:opacity-60"
      >
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
