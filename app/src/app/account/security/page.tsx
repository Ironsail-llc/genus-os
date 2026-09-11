import Link from "next/link";

import { AccountSecurityPanel } from "@/components/account-security-panel";

/**
 * Account security — the one page an operator on a local-login-only instance
 * has to visit: enrol a second factor, and change the password the installer
 * set. Deliberately minimal; the proxy behind it does the authorization.
 */
export default function AccountSecurityPage() {
  return (
    <main className="flex min-h-screen flex-col items-center bg-background p-6">
      <div className="flex w-full max-w-xl flex-col gap-6 py-10">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold tracking-tight">Account security</h1>
          <p className="text-sm text-muted-foreground">
            Two-factor authentication and password for your sign-in.
          </p>
        </div>
        <AccountSecurityPanel />
        <Link href="/" className="text-sm text-muted-foreground underline underline-offset-2">
          Back to the dashboard
        </Link>
      </div>
    </main>
  );
}

// Reads request-time auth state through the BFF; never prerender it.
export const dynamic = "force-dynamic";

export async function generateMetadata() {
  return { title: "Account security · Genus OS" };
}
