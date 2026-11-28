# Conversation Fact Extraction — Measurement

Conversation ingest keeps what a person says about themselves as facts, each
with a trust label, an event date and links to the turns it came from
([Conversation facts](../../memory-system.md#conversation-facts)). This page
measures how accurate the real extraction is on a hand-labelled set.

```bash
robothor memory-eval --extraction --extraction-model qwen3:8b --judge-model qwen3:8b \
  --concurrency 1 --report extraction.json
```

No database and no tenant: the extraction is one model call and a parse per
conversation. `--extraction-model` runs it on a named local model and is
refused while remote memory generation is on, because the remote path ignores
a named model. Without it the generation module's own model extracts, as in
ingest, and the report counts any remote calls that fell back to local.
`--repeats N` extracts every conversation N times (the extraction samples at
the generation module's default temperature, so runs differ).

Each named model is asked once, straight to Ollama, before anything else, and a
model that does not answer stops the run with exit `3`. The whole run is
benchmark traffic (`benchmark_run_scope`): an alert raised inside it, such as
"memory generation is down" when a local call fails, goes to the benchmark
digest and never to the operator.

Exit codes: `0` ran (and, with `--baseline`, did not regress), `2` regressed
against the baseline, `3` could not run (a broken label, a model that did not
answer, a judge that failed validation, a baseline for another model or split).

## The labelled set

[`conversation-facts.yaml`](conversation-facts.yaml): 49 generic conversations
(Alice, Bob, `example.com`), each one extraction window as ingest builds it.

| Category | Conversations | What it exercises |
|----------|---------------|-------------------|
| preference | 5 | Likes, dislikes, a preference given in answer to the assistant's question |
| purchase | 5 | Things bought or ordered, with a price or a choice made over several turns |
| plan | 5 | Intentions, with a month and no day |
| state_change | 5 | Old value to new value ("switched from a diesel hatchback to an electric car") |
| dated_absolute | 4 | A day stated outright ("on March 3rd", "2026-01-15") |
| dated_relative | 6 | Yesterday, N weeks ago, last Tuesday, in three weeks, a late-night turn whose UTC day is the next one, "next Friday" |
| recommendation | 5 | Lists and advice from the assistant |
| pasted | 5 | An email with headers, a `>` quotation, a forwarded message, an introduced article, and a quoted text with no marker |
| guests | 4 | Other people writing in a shared chat |
| filler | 5 | Greetings, thanks, general-knowledge questions: no facts |

80 labels, 55 of them required. A label is **required** (counted for recall)
or **allowed**: something true the extraction may keep or leave out, neither
missed nor a false positive. Pasted content is always allowed, never
required: what matters there is how it is labelled when it is kept. Turn
times are fixed in the file, so the result does not depend on the day the run
happens.

## How facts are matched

Each extracted fact is compared with each label of its conversation by the
memory eval's judge (the answer arm's, in `robothor/memory/eval_answer.py`):

1. **Rules.** The label's own sentence is a match. A sentence naming none of
   the label's `keys` is not.
2. **The LLM judge** for everything else, with the answer arm's fixed prompt:
   the label is the reference answer and the extracted sentence the answer to
   grade.

A label counts as found when any extracted fact matches it. The first match
is the one whose attributes are scored. An extracted fact counts as correct
when it matches any label, required or allowed. One that matches only labels
an earlier fact already covered is also counted as a duplicate.

**The judge is validated on every run, before anything is extracted.** It is
given planted pairs built from the labels and must get at least 95% of each
kind right, and 95% of right and wrong pooled, or the run stops:

| Planted kind | Expected | Built from |
|--------------|----------|------------|
| paraphrase | match | The label's hand-written paraphrase |
| decoy | no match | The label's hand-written close but different fact (the old value, the negation, the other person) |
| sibling | no match | Every other label of the same conversation |
| neighbour | no match | The labels of the next conversation in the same category |

## What is reported

| Metric | Meaning |
|--------|---------|
| Precision | Extracted facts that match a label, out of all extracted |
| Recall | Required labels found |
| Trust label accuracy | Matched labels stored with an acceptable origin (`user_stated`, `agent_generated`, `external`) |
| Speaker accuracy | Matched labels whose cited turns were all said by an acceptable speaker (the owner, the assistant, a guest by name) |
| Source-turn accuracy | Matched labels whose cited turns are all among the label's turns |
| Date accuracy | Matched labels whose event day is right, counting a date on an undated fact as wrong |
| Details kept | The values a label must keep (a list's items, the value something changed from) |
| Third-party facts stored as the operator's | Labels whose trust must be `external` that were extracted as `user_stated` |
| Unlabelled user-stated facts from relaying turns | Extracted facts that match no label, are stored as the operator's, and cite a turn carrying someone else's words. They need a person to read them |
| Guest claims written as the user | Extracted facts that cite only guests' turns and still say "the user" |
| Precision by stored origin | Precision of `user_stated`, `agent_generated` and `external` facts separately |
| Judge errors | Pairs the LLM judge raised on while scoring. Each is logged as a warning and counted as no match |

## The baseline, and what a regression is

[`conversation-facts-baseline.yaml`](conversation-facts-baseline.yaml) records
the measurement below for `qwen3:8b` on the dev labels: each metric pooled
over three runs, and a tolerance for each.

```bash
robothor memory-eval --extraction --extraction-model qwen3:8b --judge-model qwen3:8b \
  --concurrency 1 --repeats 2 --baseline docs/benchmarks/memory/conversation-facts-baseline.yaml
```

The run is compared metric by metric. It exits `2` when any metric moved the
wrong way by more than its tolerance: below the baseline for a rate, above it
for a count (third-party facts stored as the operator's, guest claims written
as the user, facts from filler). Counts are per run, so runs with different
`--repeats` compare.

**Tolerance** is the spread of the recorded runs, max minus min, rounded up to
a whole point for a rate and a whole count for a count, and never less than
one unit. On these three runs recall ranged from 80.0% to 90.9%, so its
tolerance is 11 points, and precision's is 8 points. A smaller regression is
not caught. The run-to-run spread at temperature 0.7 is that wide, and a
narrower tolerance would fail runs that changed nothing. More repeats would
narrow it.

**Thresholds** in the same file are **PROPOSED**, pending the operator's
agreement. Each run reports them as met or not met. They never change the
exit code while their status is `PROPOSED`:

| Threshold | Proposed |
|-----------|----------|
| Third-party facts stored as the operator's | 0 per run |
| Guest claims written as the user | 0 per run |
| Dated facts dated right | at least 90% |
| Undated facts left undated | at least 90% |
| Recall | at least 85% |
| Trust label accuracy | at least 95% |
| Precision of `user_stated` facts | at least 90% |

`--record-baseline` with `--extraction` writes a run as the new baseline (to
`--baseline`, or beside the fixtures), keeping the file's thresholds block.
Record from two or more repeats, so the tolerance has a spread to come from.
`--from-report <saved report.json>`, repeated, records from saved run reports
instead, without running the model. The verdicts are the saved ones, as the
judge gave them on those runs.

The committed file was recorded that way, from the JSON reports of the three
runs below, one alone and two as `--repeats 2`:

```bash
robothor memory-eval --extraction --record-baseline \
  --baseline docs/benchmarks/memory/conversation-facts-baseline.yaml \
  --from-report run-1.json --from-report runs-2-3.json
```

Those runs were judged with the subject-aware question of `beb6230ea`. Guest
claims written as the user were recomputed from the saved facts, since the
reports predate that metric. The thresholds block was written by hand.

## Not in this change

The measurement of hosted models, and a run on a development subset of a
public conversation-memory benchmark, are separate follow-up work. Only the
local model was run.

## Results

### 2026-11-28: `qwen3:8b`, local

Conditions: `qwen3:8b` extracts and `qwen3:8b` judges, both on a local Ollama
with remote memory generation off (0 remote calls, 0 fallbacks). The
extraction runs as ingest calls it: `think` off, the strict schema, and the
generation module's default temperature (0.7), so the runs differ. Turn times
are fixed in the labelled set, so the run date does not matter. There is no
context turn and one window per conversation. Three runs in all, one alone and
then two together (`--repeats 2`), at concurrency 1, on the harness as of
the commit that gave the judge its subject-aware question (`beb6230ea` on the
branch). A run takes about 4.5 minutes of extraction and
1.5 minutes of judge validation.

Judge validation, identical in all three runs: 79/80 planted paraphrases
matched (98.8%), and 309/314 planted wrong matches rejected (98.4%): decoys
76/80, siblings 83/84, neighbours 150/150. 80 paraphrases, 64 decoys and 17
siblings reached the model; the rest were settled by rule. Decoys sit exactly
at the 95% bar. The four the judge still accepts are a plan taken for a past
event ("flew back from Kyoto" for "is flying to Kyoto"), "booked" for "had",
"has a 3.1% account" for "is choosing between", and one subject swap ("The
user is allergic to peanuts" for Bob). With the question first used ("What fact
does the conversation record?") the judge accepted 7 of 80 decoys, most of
them a claim moved from a guest or an article's author to the user, and the
run stopped. The question now says the fact must be about the same person.

The two runs together (`--repeats 2`):

| Metric | Value |
|--------|-------|
| Precision | 61.1% (124/203) |
| Recall | 82.7% (91/110) |
| Trust label accuracy | 95.7% |
| Speaker accuracy | 100.0% |
| Source-turn accuracy | 99.1% |
| Date accuracy | 57.3% |
| Dated facts dated right | 26/35 (74.3%) |
| Undated facts left undated | 41/82 (50.0%) |
| Details kept (lists, old values) | 16/16 (100.0%) |
| Facts per conversation | 2.07 |
| Facts from filler conversations | 0 |
| Duplicates | 9 |
| Third-party facts stored as the operator's | 3 of 11 matched |
| Extraction errors | 0 |

| Category | Recall | Precision | Trust | Date | Extracted |
|----------|--------|-----------|-------|------|-----------|
| preference | 85.7% | 61.1% | 100.0% | 91.7% | 18 |
| purchase | 90.0% | 71.4% | 100.0% | 40.0% | 21 |
| plan | 80.0% | 60.9% | 100.0% | 33.3% | 23 |
| state_change | 92.9% | 81.2% | 100.0% | 15.4% | 16 |
| dated_absolute | 83.3% | 58.8% | 100.0% | 80.0% | 17 |
| dated_relative | 100.0% | 70.6% | 100.0% | 70.0% | 34 |
| recommendation | 81.2% | 71.4% | 100.0% | 53.3% | 21 |
| pasted | 50.0% | 44.0% | 50.0% | 80.0% | 25 |
| guests | 62.5% | 39.3% | 100.0% | 60.0% | 28 |
| filler | n/a | n/a | n/a | n/a | 0 |

Run to run:

| Run | Recall | Precision | Trust | Date | Extracted |
|-----|--------|-----------|-------|------|-----------|
| 1 (alone) | 90.9% (50/55) | 65.3% (66/101) | 90.8% | 55.4% | 101 |
| 2 | 80.0% (44/55) | 58.3% (60/103) | 94.7% | 61.4% | 103 |
| 3 | 85.5% (47/55) | 64.0% (64/100) | 96.7% | 53.3% | 100 |

**How to read precision.** It counts every extracted fact that matches no
label as wrong, and the labels hold little of the assistant's advice. By
stored origin, over runs 2 and 3: `user_stated` 91/104 (87.5%), `external`
9/22, `agent_generated` 24/77. Run 1's 35 unmatched facts, read one by one:

| What the unmatched fact is | Count |
|----------------------------|-------|
| True and worth keeping but not labelled (mostly the assistant's advice: "rest, ice, compression and elevation", "soft food for about a week") | 21 |
| A label the judge failed to match (the fact plus another detail, such as "wants to learn Spanish before their trip" for "wants to learn Spanish, 15 minutes a day") | 5 |
| Noise: the assistant's questions, offers and pleasantries ("one less thing to worry about") | 6 |
| A guest's claim written about the user | 3 |

Read this way run 1's precision is about 91% (92/101). Some recall is lost
the same way: the judge on real output is stricter than on the planted
paraphrases.

**What fails.**

* **Undated facts get a date.** Half the facts with no time in the
  conversation get the turn's own day as their event time (state changes
  15.4%, plans 33.3%, purchases 40%): `state-02` to `state-05` (a job change, a
  commute, a diet, a team size), `buy-01`, `buy-02`, `buy-05`, and `plan-04`.
* **Relative dates are miscounted and not corrected.** "Two weeks ago" came out
  10 days off (`rel-02`), and "in three weeks" was counted as one week, 14 days
  early (`rel-04`). Both are
  beyond the correction tolerance. "Last Tuesday" (`rel-03`) came back as the
  turn's own day, two days off and inside the tolerance, but was not
  corrected. When the model's `quote` keeps the sentence's full stop ("Last
  Tuesday I had my wisdom tooth out."),
  `relative_dates.same_statement` answers `False` and `_dated` leaves the
  date. The same quote without the full stop is corrected.
* **Guests' claims are written about the user.** Every run wrote Dana's "I'm
  vegan" as "The user is vegan" (`guest-03`), and Bob's motorbike sale and lift
  as the user's (`guest-04`). Their trust is `external`, so they are not
  anchored to the operator, but the text says "the user". In one run
  (`guest-02`), Carol's turn produced "The user prefers dark roast coffee" and
  "The user has a coffee machine". Neither is in the conversation; the first is
  the prompt's own example.
* **Third-party content stored as the operator's.** A landlord's text pasted in
  quotation marks with no marker the relay detection knows (`paste-05`) is
  stored `user_stated` in every run: "must vacate by 31 August", "deposit
  returned within 14 days".
* **The operator's own words after a relayed span are stored as external.** In
  `paste-02` ("My neighbour wrote: > … I already have a mower") the lead-in's
  span runs to the end of the turn, so "already has a mower" is `external` in
  every run.
* **Facts given in answer to the assistant lose `user_stated`.** In `buy-04`
  ("I just bought the 256 GB one" after the assistant's advice) run 1 cited the
  assistant turn too, and the fact was stored `agent_generated`.
* **Lists and merged facts.** The assistant's four board games (`rec-03`) were
  never extracted. "The sister is getting married" was folded into "is giving a
  speech at her wedding" (`abs-02`).

Nothing was extracted from filler, and every list item and changed-from value
in a matched fact was kept. The JSON reports (with every extracted fact) are
not committed. Rerun the command above to reproduce them.
