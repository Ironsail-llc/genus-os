import { signIn } from "@/lib/auth";

/**
 * Sign-in page — single "Sign in with SSO" action that kicks off the OIDC flow.
 * On success Auth.js redirects back to ``callbackUrl`` (set by the middleware).
 */
export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string }>;
}) {
  const { callbackUrl } = await searchParams;

  async function doSignIn() {
    "use server";
    await signIn("oidc", { redirectTo: callbackUrl || "/" });
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 bg-background">
      <div className="flex flex-col items-center gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Genus OS</h1>
        <p className="text-sm text-muted-foreground">Sign in to continue</p>
      </div>
      <form action={doSignIn}>
        <button
          type="submit"
          className="rounded-md bg-primary px-6 py-2.5 text-sm font-medium text-primary-foreground hover:opacity-90"
        >
          Sign in with SSO
        </button>
      </form>
    </main>
  );
}

// Avoid static prerender — depends on request-time auth config.
export const dynamic = "force-dynamic";

export async function generateMetadata() {
  return { title: "Sign in · Genus OS" };
}
