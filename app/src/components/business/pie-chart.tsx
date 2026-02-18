"use client";

import {
  PieChart as RechartsPieChart,
  Pie,
  Cell,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface PieChartProps {
  title: string;
  data: Array<{ name: string; value: number }>;
  colors?: string[];
}

const DEFAULT_COLORS = [
  "oklch(0.65 0.2 265)",
  "oklch(0.696 0.17 162.48)",
  "oklch(0.769 0.188 70.08)",
  "oklch(0.627 0.265 303.9)",
  "oklch(0.645 0.246 16.439)",
];

export function PieChart({
  title,
  data,
  colors = DEFAULT_COLORS,
}: PieChartProps) {
  return (
    <Card className="glass-panel" data-testid="pie-chart">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <RechartsPieChart>
            <Pie
              data={data}
              cx="50%"
              cy="50%"
              outerRadius={100}
              dataKey="value"
              label={({ name, percent }) =>
                `${name ?? ""} ${((percent ?? 0) * 100).toFixed(0)}%`
              }
            >
              {data.map((_, index) => (
                <Cell
                  key={`cell-${index}`}
                  fill={colors[index % colors.length]}
                />
              ))}
            </Pie>
            <Tooltip
              contentStyle={{
                backgroundColor: "oklch(0.18 0.006 285.885)",
                border: "1px solid rgba(255,255,255,0.1)",
                borderRadius: "8px",
                color: "white",
              }}
            />
            <Legend />
          </RechartsPieChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
