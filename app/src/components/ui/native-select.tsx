import * as React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Token-styled native <select>. Keeps browser/native semantics (keyboard,
 * mobile pickers, testability) while matching the design system — used
 * instead of a Radix select for simple value pickers.
 */
function NativeSelect({
  className,
  wrapperClassName,
  ...props
}: React.ComponentProps<"select"> & { wrapperClassName?: string }) {
  return (
    <span className={cn("relative inline-flex items-center", wrapperClassName)}>
      <select
        data-slot="native-select"
        className={cn(
          "h-8 appearance-none rounded-md border border-input bg-card pl-2.5 pr-8 text-sm text-foreground",
          "transition-colors hover:border-ring/40",
          "focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-1",
          "disabled:cursor-not-allowed disabled:opacity-50",
          className
        )}
        {...props}
      />
      <ChevronDown aria-hidden className="pointer-events-none absolute right-2 size-3.5 text-muted-foreground" />
    </span>
  );
}

export { NativeSelect };
