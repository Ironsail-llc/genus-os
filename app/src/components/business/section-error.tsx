import { AlertTriangle } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export interface SectionFailure {
  /** What the operator was looking at, e.g. "System health". */
  label: string;
  /** The endpoint that answered, e.g. "/api/health". */
  endpoint: string;
  /** HTTP status, when the call reached the server. */
  status?: number;
}

/**
 * Turn a thrown value into a reportable failure. An `ApiError` carries the
 * endpoint and status; anything else keeps the endpoint we were calling and
 * reports no status (the request never got an answer).
 */
export function toSectionFailure(label: string, endpoint: string, err: unknown): SectionFailure {
  if (err instanceof ApiError) {
    return { label, endpoint: err.endpoint, status: err.status };
  }
  return { label, endpoint };
}

/**
 * An inline, honest failure notice for one dashboard section.
 *
 * A card that quietly renders nothing is indistinguishable from a system with
 * nothing to show — so a failed data call says which endpoint failed and with
 * what status, and the surrounding sections keep their own data.
 */
export function SectionError({
  failure,
  className,
  testId,
}: {
  failure: SectionFailure;
  className?: string;
  testId: string;
}) {
  return (
    <div
      data-testid={testId}
      role="status"
      className={cn(
        "flex items-start gap-2 rounded-md border border-destructive/25 bg-destructive/10 px-3 py-2 text-xs text-destructive",
        className,
      )}
    >
      <AlertTriangle aria-hidden className="mt-px size-3.5 shrink-0" strokeWidth={1.75} />
      <span>
        {failure.label} unavailable ({failure.status ?? "no response"}){" "}
        <span className="font-mono text-destructive/80">{failure.endpoint}</span>
      </span>
    </div>
  );
}
