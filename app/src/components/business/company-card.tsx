"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import type { Affiliation, Company } from "@/lib/api/types";

type CompanyRef = Pick<Company, "id" | "name">;

interface CompanyCardProps {
  company: Company;
  onSelect?: (company: Company) => void;
  /** The company directly above this one. */
  parent?: CompanyRef | null;
  /** The companies directly under this one (``children`` is React's own prop). */
  subsidiaries?: CompanyRef[];
  /** People at this company and their roles. */
  affiliations?: Affiliation[];
}

function byRole(affiliations: Affiliation[]): [string, Affiliation[]][] {
  const groups = new Map<string, Affiliation[]>();
  for (const a of affiliations) {
    groups.set(a.role, [...(groups.get(a.role) ?? []), a]);
  }
  return Array.from(groups.entries());
}

export function CompanyCard({
  company,
  onSelect,
  parent,
  subsidiaries,
  affiliations,
}: CompanyCardProps) {
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
        {parent && <p data-testid="company-parent">Part of {parent.name}</p>}
        {company.domainName && <p>{company.domainName}</p>}
        {company.employees && <p>{company.employees} employees</p>}
        {company.idealCustomerProfile && (
          <Badge variant="default">ICP</Badge>
        )}
        {subsidiaries && subsidiaries.length > 0 && (
          <div className="pt-1">
            <p className="text-foreground">Includes</p>
            <ul className="list-disc pl-5" data-testid="company-children">
              {subsidiaries.map((c) => (
                <li key={c.id}>{c.name}</li>
              ))}
            </ul>
          </div>
        )}
        {affiliations && affiliations.length > 0 && (
          <div className="space-y-1 pt-1" data-testid="company-people">
            {byRole(affiliations).map(([role, people]) => (
              <p key={role} data-testid="company-role-group">
                <Badge variant="outline">{role}</Badge>{" "}
                {people.map((a) => a.personName || a.personId).join(", ")}
              </p>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
