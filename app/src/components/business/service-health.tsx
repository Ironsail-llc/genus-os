"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ServiceHealth as ServiceHealthType } from "@/lib/api/types";

interface ServiceHealthProps {
  services: ServiceHealthType[];
  overallStatus?: "ok" | "degraded";
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
            {overallStatus === "ok" ? "All Systems Operational" : "Degraded"}
          </Badge>
        )}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
        {services.map((service) => (
          <Card key={service.name} className="glass-panel" data-testid="service-card">
            <CardHeader className="pb-1 pt-3 px-3">
              <CardTitle className="text-sm font-medium flex items-center gap-2">
                <span
                  className={`w-2 h-2 rounded-full ${
                    service.status === "healthy"
                      ? "bg-green-400"
                      : "bg-red-400"
                  }`}
                />
                {service.name}
              </CardTitle>
            </CardHeader>
            <CardContent className="px-3 pb-3">
              <p className="text-xs text-muted-foreground">
                {service.responseTime !== undefined
                  ? `${service.responseTime}ms`
                  : service.status}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
