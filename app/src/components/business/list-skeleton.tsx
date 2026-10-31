import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * Placeholder rows for a list whose first read is in flight, in the shape the
 * Fleet and Runs views already use. A status region with a name, so a screen
 * reader hears what a sighted operator sees.
 */
export function ListSkeleton({
  testId,
  label,
  rows = 3,
  className,
}: {
  testId: string;
  /** Read out in place of the rows, e.g. "Loading tasks". */
  label: string;
  rows?: number;
  className?: string;
}) {
  return (
    <div
      data-testid={testId}
      role="status"
      aria-label={label}
      className={cn("flex flex-col gap-2", className)}
    >
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} aria-hidden className="rounded-lg border border-border bg-card p-3">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="mt-2 h-3 w-2/3" />
        </div>
      ))}
    </div>
  );
}
