"use client";

import {
  LineChart as RechartsLineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface LineChartProps {
  title: string;
  data: Array<Record<string, string | number>>;
  dataKey: string;
  nameKey?: string;
  color?: string;
}

export function LineChart({
  title,
  data,
  dataKey,
  nameKey = "name",
  color = "oklch(0.65 0.2 265)",
}: LineChartProps) {
  return (
    <Card className="glass-panel" data-testid="line-chart">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <RechartsLineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
            <XAxis dataKey={nameKey} stroke="rgba(255,255,255,0.5)" fontSize={12} />
            <YAxis stroke="rgba(255,255,255,0.5)" fontSize={12} />
            <Tooltip
              contentStyle={{
                backgroundColor: "oklch(0.18 0.006 285.885)",
                border: "1px solid rgba(255,255,255,0.1)",
                borderRadius: "8px",
                color: "white",
              }}
            />
            <Line
              type="monotone"
              dataKey={dataKey}
              stroke={color}
              strokeWidth={2}
              dot={{ fill: color, r: 4 }}
            />
          </RechartsLineChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
