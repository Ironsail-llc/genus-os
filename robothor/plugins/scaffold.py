"""``genus plugin new``: a plugin repository that builds a correct wheel as-is.

What the scaffold gets right so the author does not have to rediscover it:

* ``genus-plugin.yaml`` sits in the package and is listed as package data, so
  the loader finds it in the distribution's file list before importing
  anything;
* it declares the tool by contribution name AND by entry-point name, which is
  what lets ``genus plugin install`` compare the wheel to the declaration
  exactly rather than cap the verdict at ``review``;
* the agent template and the workflow directory are package data too, and the
  manifest's ``agents:`` key names the bundle, so ``genus plugin sync`` renders
  the agent onto the workspace;
* the CI workflow runs the tests, builds the wheel and prints its sha256 -- the
  hash ``genus plugin install --sha256`` and a registry entry both need;
* ``knowledge/`` holds owned, dated records (``owner``, ``source``,
  ``reviewed_on``) apart from the rules, and the ``<name>.knowledge`` doctor
  check warns on a record nobody reviewed or verified in 30 days.

Everything generated is generic: the plugin's own name and nothing else.
"""

from __future__ import annotations

import json
import keyword
import shutil
from pathlib import Path

from robothor.templates.safety import TemplateSecurityError, validate_identifier

__all__ = ["ScaffoldError", "scaffold", "tool_name"]


class ScaffoldError(ValueError):
    """A refusal, in the sentence the operator gets told."""


def package_name(name: str) -> str:
    return name.replace("-", "_")


def tool_name(name: str) -> str:
    """The example tool's name: namespaced by the package so it cannot shadow a built-in."""
    return f"{package_name(name)}_hello"


_PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
version = "{version}"
description = "{description}"
readme = "README.md"
requires-python = ">=3.11"
# Genus installs plugins with --no-deps: vendor what you need, or ask for a
# platform extra. Nothing listed here would be installed.
dependencies = []

[project.optional-dependencies]
test = ["pytest>=8", "PyYAML>=6"]

[project.entry-points."genus.tools"]
{pkg} = "{pkg}:PLUGIN"

[project.entry-points."genus.schemas"]
{pkg} = "{pkg}:PLUGIN"

[project.entry-points."genus.doctor"]
{pkg} = "{pkg}:PLUGIN"

[tool.setuptools.packages.find]
where = ["src"]

# Every directory genus-plugin.yaml may point into. A path the manifest names
# that is not package data is missing from the wheel, and sync refuses it.
[tool.setuptools.package-data]
{pkg} = [
    "genus-plugin.yaml",
    "agents/*/*.yaml",
    "agents/*/*.md",
    "workflows/*.yaml",
    "patches/*.yaml",
    "files/**/*",
    "knowledge/*.md",
    "knowledge/*.json",
]
"""

_MANIFEST = """\
# What this plugin contributes, read from the packaging layer BEFORE the module
# is imported. Keep it in step with PLUGIN in __init__.py.
name: "{name}"
contract_version: 1
handlers:
  - "{tool}"
schemas:
  - "{tool}"
# Doctor checks; the doctor requires each id to start with the plugin's name.
checks:
  - "{name}.knowledge"
# Entry-point names, declared separately from contribution names.
entry_points:
  genus.tools:
    - "{pkg}"
  genus.schemas:
    - "{pkg}"
  genus.doctor:
    - "{pkg}"

# Workspace files, as paths relative to this file. `genus plugin install` and
# `genus plugin sync` write them; `genus plugin remove` retires them.
agents:
  - agents/{name}
# workflows:                       # copied to docs/workflows/<basename>
#   - workflows/example.yaml
# patches:                         # edit an agent this plugin does not own
#   - agent: main
#     file: patches/main.patch.yaml
# files:                           # brain/**, docs/workflows/**, docs/hooks/**, docs/webhooks.yaml, docs/webhooks.d/*.yaml
#   - src: files/notes.md
#     dest: brain/{name}/notes.md
#     mode: if_missing
"""

_INIT = '''\
"""{name}: a Genus OS plugin."""

from __future__ import annotations

import asyncio
from typing import Any

from . import knowledge

__all__ = ["PLUGIN", "{tool}"]


async def {tool}(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    """Handlers take (args, ctx), the signature the engine's own tools use."""
    who = str(args.get("name") or "world")
    return {{"greeting": f"hello, {{who}}"}}


{upper}_SCHEMA = {{
    "type": "function",
    "function": {{
        "name": "{tool}",
        "description": "Say hello. Replace this with what your tool does.",
        "parameters": {{
            "type": "object",
            "properties": {{"name": {{"type": "string", "description": "Who to greet"}}}},
            "required": [],
        }},
    }},
}}



def _checks() -> dict[str, Any]:
    """Doctor checks. Empty outside the engine, where there is no doctor."""
    try:
        from robothor.doctor.model import Check, fail, ok
    except ImportError:
        return {{}}

    async def knowledge_check(_ctx: Any) -> Any:
        problems = await asyncio.to_thread(knowledge.findings)
        if problems:
            return fail(
                f"{{len(problems)}} knowledge record(s) need their owner's review: "
                + "; ".join(problems)
                + ". See knowledge/README.md"
            )
        return ok(f"every knowledge record reviewed in the last {{knowledge.STALE_AFTER_DAYS}} days")

    return {{
        "{name}.knowledge": Check(
            id="{name}.knowledge",
            title="{title}: knowledge reviewed by its owner",
            category="tools",
            severity="recommended",
            run=knowledge_check,
        ),
    }}


PLUGIN = {{
    "genus_contract_version": "1.0",
    "handlers": {{"{tool}": {tool}}},
    "schemas": {{"{tool}": {upper}_SCHEMA}},
    "read_only": ["{tool}"],
    "checks": _checks(),
}}
'''

_KNOWLEDGE = r'''"""This plugin's knowledge: owned, dated records. The layers are in knowledge/README.md.

Stdlib only, so the plugin's own tests run without the engine.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

DIR = Path(__file__).resolve().parent / "knowledge"
#: A record not verified for longer than this is reported as stale.
STALE_AFTER_DAYS = 30
REQUIRED = ("id", "name", "owner", "source")
#: Files beside the records that are not records: this guide and a load manifest.
NOT_RECORDS = ("README.md", "index.json")


def front_matter(text: str) -> dict[str, Any]:
    """The flat `key: value` block between the leading `---` lines ({} when there is none)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key[0].isspace() and not key.startswith("#"):
            value = value.strip().strip("'\"")
            fields[key.strip()] = None if value in ("", "null", "~") else value
    return {}


def records(directory: Path = DIR) -> list[tuple[str, Any]]:
    """Every knowledge record as (label, record): one per markdown file, one per JSON list item."""
    found: list[tuple[str, Any]] = []
    for path in sorted(directory.glob("*")):
        if path.name in NOT_RECORDS:
            continue
        if path.suffix == ".md":
            found.append((path.name, front_matter(path.read_text(encoding="utf-8"))))
        elif path.suffix == ".json":
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                rows = None
            if not isinstance(rows, list):
                found.append((path.name, None))
                continue
            for row in rows:
                label = row.get("id") if isinstance(row, dict) else None
                found.append((f"{path.name} ({label or '?'})", row))
    return found


def reviewed(record: Any) -> bool:
    """A record drives an action only once a person has set its reviewed_on."""
    return isinstance(record, dict) and bool(record.get("reviewed_on"))


def findings(directory: Path = DIR, today: date | None = None) -> list[str]:
    """What a person has to look at: missing fields, unreviewed records, stale records."""
    today = today or datetime.now(UTC).date()
    problems: list[str] = []
    for label, record in records(directory):
        if not isinstance(record, dict) or not record:
            problems.append(f"{label}: not a knowledge record (front matter or a JSON list of objects)")
            continue
        missing = [field for field in REQUIRED if not record.get(field)]
        if missing:
            problems.append(f"{label}: no {', '.join(missing)}")
        if not reviewed(record):
            problems.append(f"{label}: reviewed_on is null, so the record is advisory")
            continue
        verified = record.get("last_verified") or record.get("reviewed_on")
        try:
            when = date.fromisoformat(str(verified))
        except ValueError:
            problems.append(f"{label}: {verified!r} is not an ISO date")
            continue
        if (today - when).days > STALE_AFTER_DAYS:
            problems.append(f"{label}: last verified {when}, more than {STALE_AFTER_DAYS} days ago")
    return problems
'''

_KNOWLEDGE_README = """\
# Knowledge

This plugin keeps two kinds of truth apart.

| Layer | What | Who changes it | How |
|---|---|---|---|
| **Rules** | decisions, routing, guards, step order, which writes exist | engineering | code and data in this package, with a failing test first, by pull request |
| **Knowledge** | procedures in prose, wording, policies, facts about the people and systems the agent deals with | the person named as `owner` | editing the record here and setting `reviewed_on` |

Knowledge cannot add a tool, a write or a branch: that is a rule change. A rule
cannot change the business's words without a release: that is a knowledge change.

## Every knowledge file carries

| Field | What it holds |
|---|---|
| `id` | a stable identifier, lowercase with hyphens |
| `name` | what a person calls it |
| `owner` | the person or role accountable for it |
| `source` | where it came from: a dated decision, a procedure document, a meeting |
| `reviewed_on` | the ISO date its owner reviewed it, or null |
| `last_verified` | the ISO date someone last confirmed it still holds (defaults to `reviewed_on`) |

A markdown document carries them as front matter:

```markdown
---
id: returns
name: How returns work
owner: role:support-lead
source: support procedure, section 4
reviewed_on: 2026-01-15
last_verified: 2026-01-15
---

The procedure, in prose.
```

A JSON file is a list of records, each carrying the same fields (see `facts.json`).

## The rules

- **A record is advisory until `reviewed_on` is set.** It may be shown to the model
  and to people, labelled advisory; it never drives an action
  (`knowledge.reviewed(record)` is False).
- **Stale after 30 days.** The `<plugin>.knowledge` doctor check warns when a record's
  `reviewed_on` is null or its `last_verified` is more than 30 days old
  (`STALE_AFTER_DAYS` in `knowledge.py`). A warning, not an outage.
- **Only people change knowledge.** A ticket, an email, an attachment or a model's
  output never edits a record. The agent may propose a change; a person makes it.
- **Every wrong decision becomes a regression test** before the fix.
- A load manifest named `index.json` (which files each playbook loads) may sit
  beside the records; it is not a record, so it carries no front matter.
- Keep secrets and personal data out of these files: they ship in the wheel.
"""

_FACTS = """\
[
  {
    "id": "example-fact",
    "name": "An example fact",
    "owner": "role:knowledge-owner",
    "source": "replace with where this fact came from",
    "reviewed_on": null,
    "last_verified": null,
    "fact": "Replace with something the agent should know. Advisory until reviewed."
  }
]
"""

_AGENT_MANIFEST = """\
id: "{name}"
name: "{title}"
description: Replace with what this agent does
version: "0.1.0"
department: custom
instruction_file: brain/agents/{name}.md
schedule:
  cron: ""
  timezone: {{{{ timezone }}}}
delivery:
  mode: none
tools_allowed:
  - "{tool}"
"""

_INSTRUCTIONS = """\
# {title}

You are the {title} agent. Replace these instructions with what the agent does,
which tools it uses and what it reports.

Use `{tool}` when you need to greet someone.
"""

_TEST = '''\
"""The contract the engine holds this plugin to, checked without the engine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

import {pkg}
from {pkg} import PLUGIN, knowledge, {tool}

PACKAGE = Path({pkg}.__file__).parent
MANIFEST = yaml.safe_load((PACKAGE / "genus-plugin.yaml").read_text())


def test_it_speaks_the_contract_version() -> None:
    assert PLUGIN["genus_contract_version"] == "1.0"


def test_the_manifest_declares_exactly_what_the_payload_offers() -> None:
    assert set(MANIFEST["handlers"]) == set(PLUGIN["handlers"])
    assert set(MANIFEST["schemas"]) == set(PLUGIN["schemas"])


def test_every_declared_agent_is_a_template_bundle() -> None:
    for bundle in MANIFEST.get("agents") or []:
        assert (PACKAGE / bundle / "manifest.template.yaml").is_file()
        assert (PACKAGE / bundle / "instructions.template.md").is_file()


def test_the_tool_answers() -> None:
    assert asyncio.run({tool}({{"name": "Alice"}})) == {{"greeting": "hello, Alice"}}


def test_the_manifest_declares_the_doctor_checks() -> None:
    # PLUGIN["checks"] is empty outside the engine, where there is no doctor.
    if PLUGIN["checks"]:
        assert set(MANIFEST["checks"]) == set(PLUGIN["checks"])


def test_every_knowledge_record_names_its_owner_and_source() -> None:
    for label, record in knowledge.records():
        assert isinstance(record, dict), f"{{label}}: not a knowledge record"
        for field in knowledge.REQUIRED:
            assert record.get(field), f"{{label}}: no {{field}}"


def test_an_unreviewed_or_stale_record_is_reported(tmp_path) -> None:
    from datetime import date

    base = {{"id": "x", "name": "X", "owner": "role:owner", "source": "a procedure"}}
    (tmp_path / "facts.json").write_text(json.dumps([{{**base, "reviewed_on": None}}]))
    assert "advisory" in " ".join(knowledge.findings(tmp_path))
    (tmp_path / "facts.json").write_text(json.dumps([{{**base, "reviewed_on": "2026-01-01"}}]))
    assert knowledge.findings(tmp_path, today=date(2026, 1, 31)) == []
    assert "facts.json" in " ".join(knowledge.findings(tmp_path, today=date(2026, 2, 1)))
'''

_WORKFLOW = """\
name: ci

on:
  push:
  pull_request:

jobs:
  test-and-build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install
        run: python -m pip install --upgrade pip build && python -m pip install ".[test]"
      - name: Test
        run: python -m pytest -q
      - name: Build the wheel
        run: python -m build --wheel
      - name: Print the wheel's sha256
        run: sha256sum dist/*.whl
      - uses: actions/upload-artifact@v4
        with:
          name: wheel
          path: dist/*.whl
"""

_RELEASE = """\
name: release

# A vX.Y.Z tag releases the wheel and its SHA256SUMS. The tag, pyproject.toml
# and programmatic.json must name the same version, and a released version is
# never rebuilt: bump the version and tag again instead.
on:
  push:
    tags: ["v*"]

jobs:
  release:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3
      - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6.3.0
        with:
          python-version: "3.12"
      - name: Tag, pyproject.toml and programmatic.json name the same version
        run: |
          tag="${GITHUB_REF_NAME#v}"
          project=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
          listing=$(python -c 'import json; print(json.load(open("programmatic.json"))["version"])')
          if [ "$tag" != "$project" ] || [ "$tag" != "$listing" ]; then
            echo "::error::tag $GITHUB_REF_NAME, pyproject.toml $project and programmatic.json $listing must name the same version"
            exit 1
          fi
      - name: No release exists for this tag yet
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          if out=$(gh release view "$GITHUB_REF_NAME" 2>&1); then
            echo "::error::release already exists; released wheels are immutable, tag a new version"
            exit 1
          fi
          if ! printf '%s' "$out" | grep -q "release not found"; then
            echo "::error::could not check for an existing release: $out"
            exit 1
          fi
      - name: Tests pass
        run: python -m pip install ".[test]" && python -m pytest -q
      - name: Wheel builds
        run: |
          python -m pip install build
          SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) python -m build --wheel
      - name: The wheel carries its plugin manifest
        run: unzip -l dist/*.whl | grep -q 'genus-plugin.yaml'
      - name: Checksums
        run: cd dist && sha256sum -- *.whl > SHA256SUMS && cat SHA256SUMS
      - uses: softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65 # v2.6.2
        with:
          files: |
            dist/*.whl
            dist/SHA256SUMS
"""

_CLAUDE = """\
# {name}

A Genus OS plugin. Docs: `docs/PLUGINS.md` in the Genus OS repository.

## Layout

| Path | What it is |
|---|---|
| `src/{pkg}/__init__.py` | `PLUGIN`: the tool handler `{tool}` and its schema |
| `src/{pkg}/genus-plugin.yaml` | The declaration, read BEFORE import. Keep `handlers`/`schemas` equal to `PLUGIN` |
| `src/{pkg}/agents/{name}/` | Agent template bundle: `manifest.template.yaml` + `instructions.template.md` (optional `setup.yaml`) |
| `src/{pkg}/workflows/` | Workflow files; list each under `workflows:` in the manifest |
| `src/{pkg}/knowledge/` | Owned, dated knowledge records; `knowledge/README.md` says what each carries |
| `src/{pkg}/knowledge.py` | Reads the records; `findings()` is what the `{name}.knowledge` doctor check reports |
| `tests/` | Contract tests; run with `pytest` |
| `.github/workflows/ci.yml` | Tests, builds the wheel, prints its sha256 |
| `.github/workflows/release.yml` | On a `vX.Y.Z` tag: releases the wheel with its `SHA256SUMS` |
| `programmatic.json` | The listing a plugin index reads; `id` and `version` match the wheel |

## Releasing

Bump `version` in `pyproject.toml` and `programmatic.json` together, then push
a `vX.Y.Z` tag. The release workflow refuses a tag that names another version
or one already released, runs the tests, builds the wheel with fixed timestamps
(`SOURCE_DATE_EPOCH`), checks it carries `genus-plugin.yaml`, and publishes it
with `SHA256SUMS`. A released version is never rebuilt: tag a new one.

## Rules

- Every path in `genus-plugin.yaml` is relative to that file and must be
  package data. `pyproject.toml` already covers `agents/`, `workflows/`,
  `patches/` and `files/`; put new files there, or add the directory to
  `[tool.setuptools.package-data]`.
- Rules are code: a decision, a route or a write changes by pull request with a
  test. Knowledge (procedure prose, wording, facts) is a record in `knowledge/`
  with `owner`, `source` and `reviewed_on`; only a person edits it, and a record
  with no `reviewed_on` never drives an action.
- A new tool: handler + schema in `PLUGIN`, its name under `handlers` and
  `schemas` in the manifest. Name tools `{pkg}_*` so they never shadow a built-in.
- A new agent: a bundle under `agents/`, listed under `agents:`. Its
  `manifest.template.yaml` `id` must equal the directory name. Template
  variables (`{{{{ timezone }}}}`) resolve at install; operators override them in
  `.robothor/overrides/<id>.yaml`.
- `files:` may only write `brain/**`, `docs/workflows/**`, `docs/hooks/**`,
  `docs/webhooks.yaml` or `docs/webhooks.d/<name>.yaml`. Patches use
  `install:`/`remove:` sections of `set:`/`append:`/`delete:`/`detach:`.
- The plugin declares contract 1. If it uses `append:`/`detach:`, a
  `docs/webhooks.d/` drop-in or a `brain/agents/<agent>.d/` fragment, declare
  `contract_version: 2` here and `"genus_contract_version": "2.0"` in `PLUGIN`
  (and its test), so an engine that predates them refuses it loudly.
- Dependencies are not installed (`--no-deps`): vendor them.
- No personal names, emails, hosts or secrets in this repo.

## Build and install

```bash
python -m pip install ".[test]" && pytest
python -m build --wheel && sha256sum dist/*.whl
genus plugin install dist/*.whl --sha256 <hex> --accept-review
# or: pip install --no-deps --target "$ROBOTHOR_PLUGIN_DIR" dist/*.whl && genus plugin sync
```

A wheel that ships an agent (or any workspace file) is graded `review` by the
install scan; `--accept-review` is the operator saying they read it.
"""

_README = """\
# {name}

A Genus OS plugin: the `{tool}` tool and the `{name}` agent.

```bash
python -m pip install ".[test]" && pytest
python -m build --wheel
```

See `CLAUDE.md` for the layout.
"""

_GITIGNORE = """\
__pycache__/
*.egg-info/
build/
dist/
.venv/
"""


_VERSION = "0.1.0"
_DESCRIPTION = "A Genus OS plugin"


def _programmatic(name: str, title: str) -> str:
    """The listing a plugin index reads: the same id and version as the wheel."""
    listing = {
        "name": title,
        "id": name,
        "version": _VERSION,
        "format": "genus-plugin/v1",
        "description": _DESCRIPTION,
        "tags": ["genus-plugin"],
    }
    return json.dumps(listing, indent=2) + "\n"


def _files(name: str) -> dict[str, str]:
    pkg = package_name(name)
    tool = tool_name(name)
    title = name.replace("-", " ").title()
    values = {
        "name": name,
        "pkg": pkg,
        "tool": tool,
        "upper": tool.upper(),
        "title": title,
        "version": _VERSION,
        "description": _DESCRIPTION,
    }
    return {
        "pyproject.toml": _PYPROJECT.format(**values),
        "programmatic.json": _programmatic(name, title),
        "README.md": _README.format(**values),
        "CLAUDE.md": _CLAUDE.format(**values),
        ".gitignore": _GITIGNORE,
        ".github/workflows/ci.yml": _WORKFLOW,
        ".github/workflows/release.yml": _RELEASE,
        f"src/{pkg}/__init__.py": _INIT.format(**values),
        f"src/{pkg}/genus-plugin.yaml": _MANIFEST.format(**values),
        f"src/{pkg}/agents/{name}/manifest.template.yaml": _AGENT_MANIFEST.format(**values),
        f"src/{pkg}/agents/{name}/instructions.template.md": _INSTRUCTIONS.format(**values),
        f"src/{pkg}/workflows/.gitkeep": "",
        f"src/{pkg}/knowledge.py": _KNOWLEDGE,
        f"src/{pkg}/knowledge/README.md": _KNOWLEDGE_README,
        f"src/{pkg}/knowledge/facts.json": _FACTS,
        "tests/test_plugin.py": _TEST.format(**values),
    }


def scaffold(name: str, directory: str | Path | None = None) -> Path:
    """Write a new plugin repository for *name*; return its root.

    *name* is the distribution name, the agent id and -- hyphens as
    underscores -- the Python package, so it must satisfy all three: the agent
    id rule, a leading letter, and a package name that is a non-keyword
    identifier. *directory* defaults to
    ``./<name>`` and must not exist or be empty -- nothing is ever overwritten.
    """
    refusal = (
        f"{name!r} is not a usable plugin name. It becomes the distribution name, the "
        "agent id and (with hyphens as underscores) the Python package, so it must "
        "start with a letter, use only lowercase letters, digits and hyphens (at most "
        "64), and not be a Python keyword."
    )
    try:
        name = validate_identifier(name, label="plugin name")
    except TemplateSecurityError as exc:
        raise ScaffoldError(refusal) from exc
    package = package_name(name)
    if not name[0].isalpha() or not package.isidentifier() or keyword.iskeyword(package):
        raise ScaffoldError(refusal)
    from robothor.plugins.assets import reserved_agent_ids

    if name in reserved_agent_ids():
        raise ScaffoldError(
            f"{name!r} is a reserved agent id: the platform refuses a plugin that ships "
            "it. Pick another name; a plugin changes a reserved agent through "
            "`patches:` instead."
        )
    root = Path(directory) if directory is not None else Path.cwd() / name
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ScaffoldError(f"{root} exists and is not a directory.")
    if root.is_dir() and any(root.iterdir()):
        raise ScaffoldError(f"{root} is not empty; nothing was written.")
    existed = root.is_dir()
    try:
        for relative, text in _files(name).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    except BaseException:
        # Leave nothing half-written: a partial tree would make the re-run
        # refuse the directory as not empty.
        if existed:
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
        raise
    return root
