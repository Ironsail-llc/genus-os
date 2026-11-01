"use client";

import { ErrorNotice } from "@/components/error-notice";

/**
 * A route that threw while rendering. Next keeps the root layout (theme,
 * session) and renders this in the page's place; Retry asks Next to render
 * the segment again.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main className="flex min-h-svh items-center justify-center bg-background p-4">
      <ErrorNotice
        testId="route-error"
        title="This page failed to render"
        digest={error.digest}
        report={{ source: "route", message: error.message }}
        onRetry={reset}
      >
        Something threw while drawing it. Nothing has been changed by the failure. Retry, or
        report it so the server log carries the reference below.
      </ErrorNotice>
    </main>
  );
}
