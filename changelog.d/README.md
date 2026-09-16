# Release-note fragments

One file per pull request, per audience:

```
changelog.d/<PR>.<audience>.md
```

`<audience>` is one of:

| Audience | Who that is |
|----------|-------------|
| `operators` | People who run an instance — install, upgrade, backup, secrets, the doctor, paging |
| `admins` | People who configure this instance in the Helm — agents, users and roles, channels, plugins, flags |
| `agent-authors` | People who write manifests, skills and plugins against the engine |
| `internal` | Nothing a reader of any of the above can notice. Recorded here, consumed by the release, never published |

A pull request may add more than one: a change can land for operators *and*
for agent authors, and saying it twice in each reader's own words is the point.
What to avoid is the accident — **one feature filed under two audiences across
two pull requests**, so half of it lands under a heading its reader is not
reading. If a feature ships over several pull requests, give the whole feature
one audience unless you deliberately want both.

## What goes in one

Two to four sentences, written from the reader's side, naming the page they
should open next:

```markdown
You can now install a plugin from a signed registry instead of running pip by
hand. The wheel is checked against its sha256 before it is opened and an
offline scan produces a verdict that gates the install. See
[Plugins](PLUGINS.md) for what each verdict means.
```

Links are written **as they will be read**, from `docs/release-notes.md` — so
`PLUGINS.md`, not `docs/PLUGINS.md` — and must point at a page the site
actually publishes (the `exclude_docs` allowlist in `mkdocs.yml`).

No absolute paths belonging to one machine, no personal data. Name the setting,
not the box.

## The gate

CI refuses a `feat`, `fix`, `perf` or breaking-change pull request that carries
no fragment, unless it is labelled `no-changelog`:

```bash
python3 scripts/changelog_fragments.py check --pr 575 --title "fix(engine): ..." --labels "[]"
python3 scripts/changelog_fragments.py lint
```

The Release Preview sticky comment on the pull request shows the fragment text
it found, or says there is none.

## The release

semantic-release's prepare step runs:

```bash
python3 scripts/changelog_fragments.py assemble --version 1.91.0
```

which groups every fragment by audience into a dated block at the top of
`docs/release-notes.md`, deletes the fragments it consumed, and leaves both in
the release commit. A version is assembled once; a release with no fragments —
or with only `internal` ones — writes nothing rather than an empty heading.

**This file is load-bearing.** It is what keeps `changelog.d/` on disk once the
release has consumed every fragment in it, and the release tooling expands the
`changelog.d` asset only for a directory that still exists. Remove this README
and the deletions silently stop being staged, so the next release publishes
this one's entries a second time.
