"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Company } from "@/lib/api/types";

interface CompanyCardProps {
  company: Company;
  onSelect?: (company: Company) => void;
}

export function CompanyCard({ company, onSelect }: CompanyCardProps) {
  return (
    <Card
      className="glass-panel cursor-pointer hover:bg-accent/50 transition-colors"
      data-testid="company-card"
      onClick={() => onSelect?.(company)}
    >
      <CardHeader className="pb-2">
        <CardTitle className="text-base">{company.name}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm text-muted-foreground">
        {company.domainName && <p>{company.domainName}</p>}
        {company.employees && <p>{company.employees} employees</p>}
        {company.idealCustomerProfile && (
          <Badge variant="default">ICP</Badge>
        )}
      </CardContent>
    </Card>
  );
}
