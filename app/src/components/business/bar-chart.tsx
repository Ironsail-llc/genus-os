"use client";

import {
  BarChart as RechartsBarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

interface BarChartProps {
  title: string;
  data: Array<Record<string, string | number>>;
  dataKey: string;
  nameKey?: string;
  color?: string;
}

export function BarChart({
  title,
  data,
  dataKey,
  nameKey = "name",
  color = "oklch(0.65 0.2 265)",
}: BarChartProps) {
  return (
    <Card className="glass-panel" data-testid="bar-chart">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <RechartsBarChart data={data}>
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
            <Bar dataKey={dataKey} fill={color} radius={[4, 4, 0, 0]} />
          </RechartsBarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}
