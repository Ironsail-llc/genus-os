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
    // Two columns, or four once the grid's own container reaches 41rem
    // (four 9.5rem cards); never 3 + 1.
    <div className="@container">
      <div className="grid grid-cols-2 @[41rem]:grid-cols-4 gap-4" data-testid="metric-grid">
        {metrics.map((metric, i) => (
          <MetricCard key={i} {...metric} />
        ))}
      </div>
    </div>
  );
}
