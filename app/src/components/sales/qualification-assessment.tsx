export type Assessment = { criteria: Record<string, { status: "supported" | "disproved" | "unknown"; evidence_ids: string[]; explanation: string }>; research_gaps: string[] };
type Evidence = { id: string; url: string; excerpt: string };

export function QualificationAssessment({ assessment, evidence }: { assessment?: Assessment; evidence: Evidence[] }) {
  if (!assessment) return null;
  return <section aria-label="Independent qualification assessment" className="space-y-3">
    <h4 className="font-medium">Independent evidence assessment</h4>
    {Object.entries(assessment.criteria).map(([criterion, finding]) => <div key={criterion} className="border-l-2 pl-3 text-sm space-y-1">
      <p className="font-medium">{criterion.replaceAll("_", " ")}: {finding.status}</p>
      <p>{finding.explanation}</p>
      {finding.evidence_ids.map((id) => {
        const source = evidence.find((item) => item.id === id);
        return source ? <div key={id}><blockquote className="text-muted-foreground">{source.excerpt}</blockquote><a href={source.url} target="_blank" rel="noopener noreferrer" className="underline">Source {id}</a></div> : <p key={id}>Source {id} unavailable</p>;
      })}
    </div>)}
    {!!assessment.research_gaps.length && <p className="text-sm">Research gaps: {assessment.research_gaps.join("; ")}</p>}
  </section>;
}
