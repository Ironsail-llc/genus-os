export type Entry = { kind: "qualification" | "knowledge"; version: string; approved_by?: string; approved_at?: string; data: Record<string, unknown> };
const label = (value: string) => value.replaceAll("_", " ");

export function LibraryEntry({ entry, published = true }: { entry: Entry; published?: boolean }) {
  const data = entry.data;
  const weights = (data.weights ?? {}) as Record<string, number>;
  const required = (data.required ?? []) as string[];
  return <article className="rounded border p-3 space-y-2 text-sm">
    <h5 className="font-medium">{entry.kind === "knowledge" ? "Claims" : label(String(data.buying_case))} · {entry.version}</h5>
    <p className="text-muted-foreground">{published ? `Published by ${entry.approved_by} · ${new Date(entry.approved_at!).toLocaleDateString()}` : "Draft for operator review"}</p>
    {entry.kind === "qualification" ? <>
      <p>Qualification threshold: {String(data.threshold)} / 100 · Evidence age limit: {String(data.max_evidence_age_days)} days</p>
      <ul className="space-y-1">{Object.entries(weights).map(([key, points]) => <li key={key}>{label(key)}: {points} points{required.includes(key) ? " · required" : ""}{!!(data.criteria_definitions as Record<string, string> | undefined)?.[key] && <p className="text-muted-foreground">{(data.criteria_definitions as Record<string, string>)[key]}</p>}</li>)}</ul>
    </> : <dl className="space-y-3">{Object.entries((data.claims ?? {}) as Record<string, unknown>).map(([key, claim]) => <div key={key}>
      <dt className="font-medium">{label(key)}</dt>
      <dd className="whitespace-pre-wrap break-words">{typeof claim === "string" ? claim : JSON.stringify(claim, null, 2)}</dd>
    </div>)}</dl>}
    {entry.kind === "knowledge" && Object.keys(data).some((key) => key !== "claims") && <details>
      <summary className="cursor-pointer">Supporting source details</summary>
      <pre className="mt-2 whitespace-pre-wrap break-words">{JSON.stringify(Object.fromEntries(Object.entries(data).filter(([key]) => key !== "claims")), null, 2)}</pre>
    </details>}
  </article>;
}

