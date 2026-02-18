"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface MetricCardProps {
  title: string;
  value: string | number;
  description?: string;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
}

export function MetricCard({
  title,
  value,
  description,
  trend,
  trendValue,
}: MetricCardProps) {
  const trendColor =
    trend === "up"
      ? "text-green-400"
      : trend === "down"
        ? "text-red-400"
        : "text-muted-foreground";

  return (
    <Card className="glass-panel" data-testid="metric-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-bold" data-testid="metric-value">
          {value}
        </div>
        {(description || trendValue) && (
          <p className={`text-xs mt-1 ${trendColor}`}>
            {trendValue && (
              <span>
                {trend === "up" ? "↑" : trend === "down" ? "↓" : "→"}{" "}
                {trendValue}{" "}
              </span>
            )}
            {description}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
