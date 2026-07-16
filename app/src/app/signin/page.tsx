import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { oidcProviderConfigured, signIn } from "@/lib/auth";
import { CF_JWT_HEADER, cfAccessEnabled, resolveSignInMode } from "@/lib/cf-access";

/**
 * Sign-in page. When the deployment trusts Cloudflare Access and the edge
 * injected its JWT, this immediately hands off to /signin/cloudflare — the
 * operator authenticated at the edge already, so no second prompt. Otherwise
 * it renders the OIDC "Sign in with SSO" action (when configured). An `error`
 * query param always renders (never auto-redirects), breaking failure loops.
 */

const ERROR_MESSAGES: Record<string, string> = {
  CloudflareAccessFailed:
    "Cloudflare Access sign-in failed. Your identity was verified at the edge but the dashboard could not establish a session — check the bridge exchange and account binding.",
  CloudflareAccessUnavailable:
    "Cloudflare Access sign-in is not available for this request.",
};

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string; error?: string }>;
}) {
  const { callbackUrl, error } = await searchParams;
  const requestHeaders = await headers();
  const cfEnabled = cfAccessEnabled();
  const hasCfHeader = Boolean(requestHeaders.get(CF_JWT_HEADER));

  const mode = resolveSignInMode({
    hasCfHeader,
    cfEnabled,
    oidcConfigured: oidcProviderConfigured(),
    errorParam: error,
  });

  if (mode === "cf-redirect") {
    redirect(`/signin/cloudflare?callbackUrl=${encodeURIComponent(callbackUrl || "/")}`);
  }

  async function doSignIn() {
    "use server";
    await signIn("oidc", { redirectTo: callbackUrl || "/" });
  }

  const errorMessage = error
    ? (ERROR_MESSAGES[error] ?? "Sign-in failed. Please try again.")
    : null;

  return (
    <main className="flex min-h-screen flex-col items-center justify-center bg-background p-6">
      <div className="glass-panel flex w-full max-w-sm flex-col items-center gap-6 px-8 py-10">
        <div className="flex flex-col items-center gap-3">
          <span
            aria-hidden
            className="flex size-10 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-brand-2 text-white shadow-lg shadow-primary/20"
          >
            <svg viewBox="0 0 24 24" fill="currentColor" className="size-5">
              <path d="M13 2 4.5 13.5h5L8 22l8.5-11.5h-5L13 2z" />
            </svg>
          </span>
          <div className="flex flex-col items-center gap-1">
            <h1 className="text-xl font-semibold tracking-tight">Genus OS</h1>
            <p className="text-sm text-muted-foreground">Sign in to continue</p>
          </div>
        </div>
        {errorMessage && (
          <p className="text-center text-sm text-destructive">{errorMessage}</p>
        )}
        {mode === "oidc-button" ? (
          <form action={doSignIn} className="w-full">
            <button
              type="submit"
              className="w-full rounded-md bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground transition-[filter] hover:brightness-110 focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2"
            >
              Sign in with SSO
            </button>
          </form>
        ) : (
          !errorMessage && (
            <p className="text-center text-sm text-muted-foreground">
              {cfEnabled && !hasCfHeader
                ? "This deployment signs in through its access-protected hostname. Open the dashboard via its public URL."
                : "No sign-in method is configured for this deployment."}
            </p>
          )
        )}
      </div>
    </main>
  );
}

// Avoid static prerender — depends on request-time auth config and headers.
export const dynamic = "force-dynamic";

export async function generateMetadata() {
  return { title: "Sign in · Genus OS" };
}
