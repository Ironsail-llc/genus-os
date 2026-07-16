"use client";

import { Skeleton } from "@/components/ui/skeleton";

/** Loading placeholder mirroring DefaultDashboard's greeting + metric grid layout. */
export function WelcomeSkeleton() {
  return (
    <div className="p-6 space-y-6" data-testid="welcome-skeleton" aria-hidden>
      {/* Greeting */}
      <div className="space-y-2">
        <Skeleton className="h-8 w-64 rounded-lg" />
        <Skeleton className="h-4 w-48" />
      </div>

      {/* Metric tiles — same glass-panel grid the real dashboard renders */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {[1, 2, 3].map((i) => (
          <div key={i} className="glass-panel p-4 space-y-2">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-7 w-16" />
            <Skeleton className="h-3 w-28" />
          </div>
        ))}
      </div>

      {/* Content panels */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {[1, 2].map((i) => (
          <div key={i} className="rounded-lg border border-border bg-card p-4 space-y-3">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-4/6" />
          </div>
        ))}
      </div>
    </div>
  );
}
