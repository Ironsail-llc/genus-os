"use client";

import { useState, type ReactNode } from "react";
import { AlertTriangle, RefreshCw, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { reportDashboardError } from "@/lib/dashboard/error-reporter";
import { cn } from "@/lib/utils";

/**
 * The one way the dashboard says "this part failed to draw".
 *
 * Same voice as `SectionError`: name what failed, say plainly that nothing was
 * changed, and give the two things an operator can do — try again, or send the
 * failure to the server log so someone can find it. Report posts once; a
 * second click would only be a second copy of the same line.
 */
export function ErrorNotice({
  testId,
  title,
  children,
  digest,
  report,
  onRetry,
  className,
}: {
  testId: string;
  title: string;
  children?: ReactNode;
  /** Next's server-side error digest, when there is one: the key to the server log line. */
  digest?: string;
  /** What Report sends to `/api/dashboard/error`. */
  report: { source: string; message: string };
  onRetry: () => void;
  className?: string;
}) {
  const [reported, setReported] = useState(false);

  const sendReport = () => {
    if (reported) return;
    setReported(true);
    reportDashboardError(report.source, report.message, digest ? { digest } : undefined);
  };

  return (
    <div
      data-testid={testId}
      role="alert"
      className={cn(
        "flex max-w-xl flex-col gap-3 rounded-lg border border-destructive/25 bg-destructive/10 p-4 text-destructive",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" strokeWidth={1.75} />
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">{title}</p>
          {children ? <div className="text-xs text-muted-foreground">{children}</div> : null}
          {digest ? (
            <p className="font-mono text-xs text-destructive/80">Reference {digest}</p>
          ) : null}
        </div>
      </div>
      <div className="flex gap-2">
        <Button type="button" size="sm" variant="outline" onClick={onRetry}>
          <RefreshCw aria-hidden className="size-3.5" />
          Retry
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={sendReport} disabled={reported}>
          <Send aria-hidden className="size-3.5" />
          {reported ? "Reported" : "Report"}
        </Button>
      </div>
    </div>
  );
}
