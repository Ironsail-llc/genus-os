"use client";

import "./globals.css";
import { ErrorNotice } from "@/components/error-notice";

/**
 * The root layout itself threw. Nothing above this survives (no theme, no
 * session), so it draws its own document, dark like the dashboard's default.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en" className="dark">
      <body className="antialiased">
        <main className="flex min-h-svh items-center justify-center bg-background p-4">
          <ErrorNotice
            testId="global-error"
            title="The Helm failed to start"
            digest={error.digest}
            report={{ source: "global", message: error.message }}
            onRetry={reset}
          >
            The page frame threw before anything could be drawn. Nothing has been changed by the
            failure. Retry, or report it so the server log carries the reference below.
          </ErrorNotice>
        </main>
      </body>
    </html>
  );
}
