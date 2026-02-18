"use client";

export function WelcomeSkeleton() {
  return (
    <div className="p-6 space-y-6 animate-pulse" data-testid="welcome-skeleton">
      {/* Greeting skeleton */}
      <div className="space-y-2">
        <div className="h-8 w-64 bg-zinc-800 rounded-lg" />
        <div className="h-4 w-48 bg-zinc-800/60 rounded" />
      </div>

      {/* Metric cards skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {[1, 2, 3].map((i) => (
          <div
            key={i}
            className="p-4 rounded-xl border border-zinc-800 space-y-2"
          >
            <div className="h-3 w-20 bg-zinc-800/60 rounded" />
            <div className="h-7 w-16 bg-zinc-800 rounded" />
            <div className="h-3 w-28 bg-zinc-800/40 rounded" />
          </div>
        ))}
      </div>

      {/* Content area skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="p-4 rounded-xl border border-zinc-800 space-y-3">
          <div className="h-4 w-32 bg-zinc-800/60 rounded" />
          <div className="h-3 w-full bg-zinc-800/30 rounded" />
          <div className="h-3 w-5/6 bg-zinc-800/30 rounded" />
          <div className="h-3 w-4/6 bg-zinc-800/30 rounded" />
        </div>
        <div className="p-4 rounded-xl border border-zinc-800 space-y-3">
          <div className="h-4 w-32 bg-zinc-800/60 rounded" />
          <div className="h-3 w-full bg-zinc-800/30 rounded" />
          <div className="h-3 w-5/6 bg-zinc-800/30 rounded" />
          <div className="h-3 w-4/6 bg-zinc-800/30 rounded" />
        </div>
      </div>
    </div>
  );
}
