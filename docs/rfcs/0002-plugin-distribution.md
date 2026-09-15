# RFC 0002 — Plugin distribution: a signed static index, install, and a scan verdict

**Status:** shipped
**Supersedes:** RFC #267 (plugin architecture), which is closed unimplemented
**Date:** 2026-09-15

## The problem

Genus had a plugin *seam* and no plugin *distribution*. Twelve entry-point
groups, a manifest a distribution must ship, a lockfile recording what an
operator accepted — and exactly one way to get a plugin onto a box:

```
pip install some-plugin
```

Which is to say: whatever PyPI resolves today, plus its entire dependency
closure, with no signature, no hash, no scan, and no record of where any of it
came from. Every `verdict` in `plugins.lock` read `unscanned`, honestly,
because nothing scanned anything.

Three specific holes:

1. **No provenance.** Nothing said a wheel was the one its author published.
   A typosquat, a compromised PyPI account or a hostile mirror all look
   identical to a correct install.
2. **No review.** A plugin contributing a `genus.channels` entry point becomes
   the delivery surface for every briefing the moment it is installed. Nothing
   told the operator that before it happened.
3. **No dependency boundary.** `pip install` resolves the plugin's declared
   dependencies from the public index. A plugin's own metadata could name the
   next download, which is the supply chain OpenClaw lost.

## What shipped

### The registry is a signed static file

`index.json` plus a detached `index.json.sig`. No server.

A package server is a thing to run, patch, authenticate against and lose. Two
files can be served by GitHub Pages, an S3 bucket, a company's own nginx, or
copied onto a box with no network at all — and every one of those mirrors is
exactly as trustworthy as the original, because trust comes from the signature
and the pinned key, never from the host. That is the property a package server
does not have, and it is why this is a better fit for an appliance than a
smaller copy of PyPI would be.

The schema is in `robothor/plugins/registry.py` and documented for consumers in
[`docs/PLUGINS.md`](../PLUGINS.md). The load-bearing decision is **what gets
signed**: the *canonical* form of the whole document — JSON, sorted keys, no
whitespace — and the served file must already be those bytes. Verifying a
re-canonicalised download instead would make the signature cover the *parse*
rather than the bytes, and accept every difference a JSON parser normalises
away: duplicate keys, `1.0` for `1`, reordered members.

### Keys are pinned out of band, in two places

- `robothor/plugins/registry_keys.py` — what the platform itself vouches for.
  **It ships empty.** The production key for the Genus registry is minted by
  whoever runs that registry; a placeholder committed before it exists either
  never gets replaced or gets replaced by whoever opens the next pull request.
- `ROBOTHOR_PLUGIN_INDEX_KEYS` — a directory of PEM public keys on the
  instance, each file's stem being the `key_id` it pins. This is what makes a
  private company registry an operator decision rather than a platform change.

`ROBOTHOR_PLUGIN_INDEXES` is the ordered list of index URLs. First index
publishing a name wins, so a company index listed first shadows the platform's
on purpose — but a name published by **two different publishers** is refused
until the operator names one with `--index`. Resolving that quietly would be
name-squatting with the platform's help.

### Every refusal is an attack with a name

| refused | why it is an attack |
|---|---|
| bad or missing signature | the whole point |
| `key_id` nobody pinned | otherwise the document names its own authority |
| signature `key_id` ≠ document `key_id` | pasting a trusted key id onto someone else's document |
| `schema` ≠ 1 | a future format read by today's rules is a guess |
| `generated_at` > 90 days old | a mirror serving a pre-yank index undoes the yank, silently |
| `generated_at` in the future | how a stale index is kept "fresh" forever |
| redirect off the origin | a pinned URL that can be redirected is not pinned |
| body > 1 MB | a static index is metadata for a few hundred plugins |
| non-canonical bytes | see above — the signature would cover the parse |

### The install pipeline

Order is the design. Each step is a refusal, and the step that executes
anything is last.

1. **resolve** — an entry in a verified index, or a wheel the operator hashed
   themselves with `--sha256`
2. **verify** — the index signature against a pinned key (registry path only;
   an explicit wheel is the operator vouching in person, which is why the hash
   is mandatory there)
3. **download** — to a temp dir, size-capped at 50 MB, sha256 must equal what
   the signed index pinned
4. **open** — bounded zip extraction (`robothor/plugins/wheel.py`): no symlink
   member, no absolute or traversing path, no duplicate names, member and
   uncompressed-size caps, all checked against the headers before a byte is
   written
5. **declare** — `genus-plugin.yaml` must be present and its sha256 must equal
   the index's `manifest_sha256`. A declaration widened after publication is
   refused *here*, rather than caught by the lockfile's drift check one import
   too late
6. **scan** — `robothor/plugins/scan.py`, on the bytes actually downloaded.
   `blocked` refuses; `review` refuses without `--accept-review`; `safe`
   proceeds
7. **install** — pip, as a list, `shell=False`
8. **record** — `sync()`, then the row's verdict, artifact hash and source

### pip is allowed to do nothing

```
python -m pip install --no-deps --no-index --find-links <our temp dir> name==version
```

`--no-index` plus `--find-links` on our own staging directory means pip
resolves nothing and reaches nowhere; it installs exactly the one file that
survived steps 3–6. `--no-deps` is what stops the plugin's own metadata naming
the next download. pip never sees a URL, an `--index-url` or `--pre`.

The honest consequence, documented rather than hidden: **a plugin that needs a
dependency the platform does not already ship must vendor it, or ask for it to
be added to the platform's extras.** That is a real constraint on plugin
authors and it is the right trade — an installer that pulls arbitrary packages
on a plugin's say-so would close nothing.

`ROBOTHOR_PLUGIN_DIR` adds `--target` for images whose `site-packages` is
read-only.

### The scan verdict

Pure, offline, deterministic, and an **AST walk rather than a grep** — a
docstring reading "never call `eval()`" must not block an install, and
`getattr(builtins, "ex" + "ec")` must. A text scan gets both backwards, and the
first false positive is the last time anybody reads a verdict.

**blocked** — undeclared surface (a `genus.*` group the manifest declares
nothing for), a claimed built-in name, a contract version this engine does not
speak, `os.system()`, `shell=True`, `eval`/`exec`/`compile` on non-literal
input, a decoder feeding `exec`, `ctypes`, a raw socket, or a write under
`/etc`, an SSH directory or a path `robothor.engine.secret_paths` calls a
credential.

**review** — the groups that run with no tool call (`hooks`, `guardrails`,
`sandboxes`, `channels`, `memory`, `jobs`), a network import, a direct
`os.environ` read, shipped prompt text, and **anything the scan could not
read**: an unparseable file, one over the size cap, or a wheel with more files
than the scan bound. "We did not look" never renders as "safe" — that is the
inert-control failure this platform keeps shipping.

**safe** — everything else.

Prompt text is `static-only` by default: the bytes were noticed, not read.
`--scan-prompts` runs the platform's existing injection screen over them; a
screen that cannot run reports `static-only` with a sentence rather than
silence.

### The lockfile learned where a plugin came from

`LockRow` gains an additive `source`:

```json
"source": {
  "origin": "registry",
  "index_url": "https://…/index.json",
  "publisher_key_id": "genus-2026",
  "installed_at": "2026-09-15T00:00:00+00:00"
}
```

Old lockfiles parse unchanged (pinned by a test — an upgrade that made every
existing lockfile unreadable would re-enable every plugin an operator had
turned off), `sync` carries it forward like `enabled` and `verdict`, and it is
written only when set.

Its **absence** is the load-bearing half: a row with no `source` came from
`genus plugin sync` recording something an operator pip-installed by hand, and
`genus plugin remove` refuses those without `--force`. Uninstalling a package
the platform did not install, because it happens to appear in the platform's
lockfile, is the platform reaching outside what it owns.

## What is deliberately not done

- **No pip index.** No `--index-url`, no `--extra-index-url`, no URL handed to
  pip, ever. The installer is the downloader precisely so the signature and the
  hash are checked by code that knows about them.
- **No dependency resolution.** `--no-deps`, permanently. See above.
- **No HTTP.** Indexes and artifacts must be `https`. The signature protects
  the contents either way, but a plaintext fetch still leaks which plugins an
  instance is looking at.
- **No wheel path or URL over HTTP.** `POST /api/plugins/install` takes a
  distribution name and nothing that could become a path — a dashboard naming a
  filesystem path would be a file read on the engine's box, and one naming a
  URL would make the engine fetch on a caller's say-so. Offline wheel installs
  stay on the CLI, where the operator is standing at the machine.
- **No hosting.** The `genus-plugins` repository and the production signing key
  do not exist yet; both are the operator's to create. `registry_keys.py` ships
  empty and `DEFAULT_INDEX_URL` points at where that repository's Pages site
  will be.
- **No sandbox.** A plugin that passes the scan still runs with the daemon's
  privileges once imported. The scan raises the cost of hiding something and
  names what it found; it is not a proof and does not claim to be.
- **No signing from inside the platform.** `scripts/build_plugin_index.py` is
  handed a private key path by whoever runs it and stores nothing.

## Relationship to RFC #267

RFC #267 (*plugin architecture v1 — governed plugins on open standards*) is a
design-only pull request, still open, proposing a much wider surface than this
one. Its RFC document has never been merged, so it exists only on that branch.
Being precise about the difference matters, because closing it on a vague
summary would lose the design without saying so.

**What #267 proposed:**

| | status here |
|---|---|
| tool plugins = **MCP servers**, formalising the existing `adapters.py` / `mcp_client.py` mount path | **not delivered.** Adapters stay exactly as they are; nothing here installs, signs or scans an MCP server |
| skill plugins = **agentskills.io `SKILL.md` directories**, packaged and namespaced, `tools_required` never exceeding the agent's grants | **not delivered.** The only thing this touches is that a wheel shipping prompt text is a `review` reason |
| engine plugins = Python entry points (group `genusos.plugins`) | **shipped, differently.** The seam that exists uses twelve `genus.*` groups, and this RFC is its distribution half |
| a fail-closed `genus-plugin.yaml` **permission manifest**, each clause mapped to an existing enforcing subsystem | **partly.** `genus-plugin.yaml` ships and is enforced, but it declares *contributions*, not permissions. There are no permission clauses |
| **three sandbox tiers** (trusted / subprocess / rootless-podman) | **deferred.** See "no sandbox" above: a plugin that passes the scan runs with the daemon's privileges |
| a versioning contract and zero-break grandfathering of all three extension surfaces | **shipped for the entry-point surface** (`CONTRACT_VERSION`, `manifest_mode`'s observe ladder) |
| an **OpenClaw importer** | **dropped.** No importer, and none planned |
| a lockfile with hash pinning and explicit operator enable | **shipped** (`robothor/plugins/lockfile.py`, #566) |

**Its five open questions, answered as they were asked:**

1. **Tenancy of enablement** — *dropped, deliberately.* One engine process runs
   one set of plugins, so there is nothing per-tenant to enable. `/api/plugins`
   refuses a non-primary tenant outright rather than pretending otherwise: a
   second tenant disabling a plugin would be taking a capability out of
   somebody else's instance.
2. **The trusted-tier signing bar** — *answered by removing the tiers.* There
   is one bar and it applies to everything: the index is Ed25519-signed and the
   key is pinned out of band. "Who is trusted" is therefore whose key the
   operator put in `ROBOTHOR_PLUGIN_INDEX_KEYS` — an operator decision, not a
   platform curation decision, which is also why the platform ships no key.
3. **Skill self-improvement scope** — *not answered; deferred with the skill
   plugins themselves.* Nothing here packages, installs or governs a
   `SKILL.md`. This question comes back whole the day skill plugins do.
4. **The guardrail-relaxation ban** — *partly answered, and worth being blunt.*
   No manifest clause can relax a guardrail, because there are no permission
   clauses in this design at all. But a distribution can still contribute to
   `genus.guardrails`, and once imported that policy runs inside the engine.
   That is precisely why contributing to `guardrails`, `hooks`, `sandboxes`,
   `channels`, `memory` or `jobs` is a `review` verdict the operator has to
   accept out loud. The surface is named and gated; it is not banned.
5. **OpenClaw importer phasing** — *dropped.* There is no importer.

So this RFC supersedes #267's **distribution** half and defers or drops the
rest. #267 should be closed pointing here, with the four undelivered pieces
(MCP tool plugins, `SKILL.md` skills, sandbox tiers, the importer) named — each
is a separate piece of work, and none of them is blocked by anything shipped
here.

## Open, and whose

- Create the `genus-plugins` repository, mint the production Ed25519 key, add
  its public half to `registry_keys.py`. **Operator.**
- ~~The Helm's install UI reads the plan and verdict shapes above.~~
  **Shipped.** Settings › Plugins installs from the registry (preview →
  findings → explicit acceptance → install) and removes a distribution this
  platform put there; see "From the Helm" in
  [`docs/PLUGINS.md`](../PLUGINS.md). The listing gained `lockfile.problem`,
  `plugins[].source`, `indexes` and `failures[].distribution` to make it
  possible without the page guessing.
- MCP tool plugins, `SKILL.md` skill plugins and the sandbox tiers, if they are
  still wanted. **Separate work, from #267.**
- A prompt screen tuned for documentation rather than for an assembled agent
  prompt. `--scan-prompts` reuses the unattended-run screen, which is measurably
  noisy over ops documentation (see PLUGINS.md); its findings are `review`
  reasons, never blocks.
