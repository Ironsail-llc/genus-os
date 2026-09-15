/**
 * Three headings for the governed flags, derived rather than tabulated.
 *
 * Twenty-one flags in one flat list is a page an operator scrolls rather than
 * reads, and the three groupings that mean something are: the safety ladders
 * (a rung that can BLOCK), the RIP family (the numbered engine upgrades, whose
 * names carry their own family), and everything else.
 *
 * The rule is computed from the schema, never from a list of names kept here.
 * A hand-written name list is the defect this repo has shipped three times —
 * `robothor/flags/store.py::governed_flags`'s own header records the last one,
 * where sixteen of twenty names in a hand-written set were declared in no
 * settings field at all — and its failure mode is always that a new flag
 * renders nowhere. Here the last bucket is a catch-all, so a governed flag
 * that matches no rule still appears; the worst case is a heading somebody
 * later decides is wrong, rather than a control the operator cannot see.
 */

import type { SettingField } from "@/lib/settings/schema";

export type FlagSectionId = "guardrails" | "ladders" | "tuning";

export interface FlagSection {
  id: FlagSectionId;
  label: string;
  /** One sentence on what the flags under this heading have in common. */
  blurb: string;
  fields: SettingField[];
}

/** `ROBOTHOR_RIP_<n>_…` — the numbered engine upgrades, inventoried in `infra/flags.yaml`. */
const RIP = /^ROBOTHOR_RIP_\d+_/;

/** The rung that makes a flag a guardrail: one of its values BLOCKS something. */
const BLOCKING_RUNG = "enforce";

const ORDER: Array<{ id: FlagSectionId; label: string; blurb: string }> = [
  {
    id: "guardrails",
    label: "Guardrails",
    blurb:
      "Checks with a rung that blocks. Moving one to enforce changes what the engine refuses, so read the verdict first: a control that has never fired is not evidence that it is safe to promote.",
  },
  {
    id: "ladders",
    label: "Engine ladders",
    blurb:
      "The numbered engine upgrades. Each one is inventoried with an owner and a promotion date in infra/flags.yaml.",
  },
  {
    id: "tuning",
    label: "Tuning",
    blurb: "Everything else an operator governs live — switches with no enforcement rung.",
  },
];

function sectionFor(field: SettingField): FlagSectionId {
  if (RIP.test(field.name)) return "ladders";
  if (field.choices?.includes(BLOCKING_RUNG)) return "guardrails";
  return "tuning";
}

/**
 * The governed fields, bucketed. Declaration order is kept inside a section,
 * and an empty section is not rendered.
 */
export function flagSections(fields: SettingField[]): FlagSection[] {
  const buckets = new Map<FlagSectionId, SettingField[]>();
  for (const field of fields) {
    if (!field.governed) continue;
    const id = sectionFor(field);
    const bucket = buckets.get(id);
    if (bucket) bucket.push(field);
    else buckets.set(id, [field]);
  }
  return ORDER.filter((section) => (buckets.get(section.id)?.length ?? 0) > 0).map((section) => ({
    ...section,
    fields: buckets.get(section.id) ?? [],
  }));
}
