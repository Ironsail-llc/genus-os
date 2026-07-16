import { cn } from "@/lib/utils";

export type Status = "ok" | "running" | "degraded" | "failing" | "idle";

const STATUS_STYLES: Record<Status, { label: string; text: string; dot: string }> = {
  ok: { label: "Healthy", text: "text-success", dot: "bg-success" },
  running: { label: "Running", text: "text-info", dot: "bg-info animate-pulse" },
  degraded: { label: "Degraded", text: "text-warning", dot: "bg-warning" },
  failing: { label: "Failing", text: "text-destructive", dot: "bg-destructive" },
  idle: { label: "Idle", text: "text-muted-foreground", dot: "bg-muted-foreground/60" },
};

/** Map raw engine/bridge status strings onto the five semantic statuses. */
export function fromEngineStatus(status?: string | null): Status {
  switch (status?.toLowerCase()) {
    case "completed":
    case "success":
    case "ok":
    case "healthy":
      return "ok";
    case "running":
    case "in_progress":
    case "started":
      return "running";
    case "degraded":
    case "timeout":
    case "partial":
      return "degraded";
    case "failed":
    case "error":
    case "crashed":
      return "failing";
    default:
      return "idle";
  }
}

export function StatusBadge({
  status,
  label,
  className,
}: {
  status: Status;
  label?: string;
  className?: string;
}) {
  const s = STATUS_STYLES[status] ?? STATUS_STYLES.idle;
  return (
    <span
      data-testid="status-badge"
      className={cn("inline-flex items-center gap-1.5 text-xs font-medium", s.text, className)}
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", s.dot)} />
      {label ?? s.label}
    </span>
  );
}
