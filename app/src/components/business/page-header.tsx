import { cn } from "@/lib/utils";

export function PageHeader({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div data-testid="page-header" className={cn("flex items-baseline gap-3", className)}>
      <h2 className="text-base font-semibold tracking-tight text-foreground">{title}</h2>
      {description && <span className="text-xs text-muted-foreground/80">{description}</span>}
      {children && <div className="ml-auto flex items-center gap-2">{children}</div>}
    </div>
  );
}
