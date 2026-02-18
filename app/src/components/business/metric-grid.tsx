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
      className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4"
      data-testid="metric-grid"
    >
      {metrics.map((metric, i) => (
        <MetricCard key={i} {...metric} />
      ))}
    </div>
  );
}
