"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import type { Affiliation, Person } from "@/lib/api/types";
import { isoDate } from "./affiliation-dates";

interface ContactCardProps {
  person?: Person;
  /** The person's roles at companies, active and ended. */
  affiliations?: Affiliation[];
  loading?: boolean;
  onSelect?: (person: Person) => void;
}

export function ContactCard({ person, affiliations, loading, onSelect }: ContactCardProps) {
  if (loading || !person) {
    return (
      <Card className="glass-panel cursor-pointer" data-testid="contact-card-skeleton">
        <CardHeader className="pb-2">
          <Skeleton className="h-5 w-32" />
        </CardHeader>
        <CardContent className="space-y-2">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-4 w-24" />
        </CardContent>
      </Card>
    );
  }

  const fullName = `${person.name.firstName} ${person.name.lastName}`.trim();

  return (
    <Card
      className="glass-panel cursor-pointer hover:bg-accent/50 transition-colors"
      data-testid="contact-card"
      onClick={() => onSelect?.(person)}
    >
      <CardHeader className="pb-2">
        <CardTitle className="text-base">{fullName}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1 text-sm text-muted-foreground">
        {person.email && <p>{person.email}</p>}
        {person.jobTitle && <p>{person.jobTitle}</p>}
        {person.company?.name && (
          <Badge variant="secondary">{person.company.name}</Badge>
        )}
        {person.city && <p>{person.city}</p>}
        {affiliations && affiliations.length > 0 && (
          <ul className="space-y-1 pt-1" data-testid="contact-affiliations">
            {affiliations.map((a) => (
              <li key={a.id} data-testid="affiliation-row" className="flex flex-wrap items-center gap-1">
                <span className="text-foreground">{a.companyName || a.companyId}</span>
                <Badge variant="outline">{a.role}</Badge>
                {a.since && <span>since {isoDate(a.since)}</span>}
                {!a.isActive && (
                  <>
                    <Badge variant="secondary">Inactive</Badge>
                    {a.until && <span>ended {isoDate(a.until)}</span>}
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
