"use client";

import { MetricCard } from "./metric-card";

interface Metric {
  title: string;
  value: string | number;
  description?: string;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
}

interface MetricGridProps {
  metrics: Metric[];
}

export function MetricGrid({ metrics }: MetricGridProps) {
  return (
    <div
      className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,9.5rem),1fr))] gap-4"
      data-testid="metric-grid"
    >
      {metrics.map((metric, i) => (
        <MetricCard key={i} {...metric} />
      ))}
    </div>
  );
}
