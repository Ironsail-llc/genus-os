"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ServiceHealth as ServiceHealthType, ServiceStatus } from "@/lib/api/types";

interface ServiceHealthProps {
  services: ServiceHealthType[];
  overallStatus?: "ok" | "degraded";
}

/**
 * Four states, each with its own word and colour. "Off" is calm grey: a
 * service the operator switched off is not a problem and must not read as one.
 */
const STATE: Record<ServiceStatus, { word: string; dot: string; card: string }> = {
  healthy: { word: "Healthy", dot: "bg-green-400", card: "bg-emerald-500/[0.02]" },
  degraded: { word: "Degraded", dot: "bg-amber-400", card: "bg-amber-500/[0.05]" },
  unhealthy: { word: "Down", dot: "bg-red-400", card: "bg-red-500/[0.04]" },
  disabled: { word: "Off", dot: "bg-muted-foreground/40", card: "bg-muted/20" },
};

function LatencyBar({ ms }: { ms?: number }) {
  if (ms === undefined) return null;
  const pct = Math.min(100, (ms / 200) * 100);
  const color =
    ms < 50 ? "bg-emerald-400" : ms < 200 ? "bg-amber-400" : "bg-red-400";
  return (
    <div className="w-full h-1 rounded-full bg-muted/50 mt-1" data-testid="latency-bar">
      <div
        className={`h-full rounded-full ${color}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export function ServiceHealth({ services, overallStatus }: ServiceHealthProps) {
  return (
    <div data-testid="service-health">
      <div className="flex items-center gap-2 mb-4">
        <h3 className="font-medium">Service Health</h3>
        {overallStatus && (
          <Badge
            variant={overallStatus === "ok" ? "default" : "destructive"}
            data-testid="overall-status"
          >
            {overallStatus === "ok" ? "All systems operational" : "Degraded"}
          </Badge>
        )}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {services.map((service) => {
          const state = STATE[service.status] ?? STATE.unhealthy;
          return (
            <Card
              key={service.name}
              className={`glass-panel ${state.card}`}
              data-testid="service-card"
              data-service={service.name}
              data-status={service.status}
            >
              <CardHeader className="pb-1 pt-3 px-3">
                <CardTitle className="text-sm font-medium flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${state.dot}`} />
                  {service.label ?? service.name}
                </CardTitle>
              </CardHeader>
              <CardContent className="px-3 pb-3">
                <p className="text-xs text-muted-foreground">
                  {state.word}
                  {service.status !== "disabled" && service.responseTime !== undefined
                    ? ` · ${service.responseTime}ms`
                    : ""}
                </p>
                {service.detail && (
                  <p className="text-[11px] text-muted-foreground/80 mt-0.5 break-words">
                    {service.detail}
                  </p>
                )}
                {service.status !== "disabled" && <LatencyBar ms={service.responseTime} />}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
