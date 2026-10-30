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
  hash ``genus plugin install --sha256`` and a registry entry both need.

Everything generated is generic: the plugin's own name and nothing else.
"""

from __future__ import annotations

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
version = "0.1.0"
description = "A Genus OS plugin"
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
# Entry-point names, declared separately from contribution names.
entry_points:
  genus.tools:
    - "{pkg}"
  genus.schemas:
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

from typing import Any

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

PLUGIN = {{
    "genus_contract_version": "1.0",
    "handlers": {{"{tool}": {tool}}},
    "schemas": {{"{tool}": {upper}_SCHEMA}},
    "read_only": ["{tool}"],
}}
'''

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
from pathlib import Path

import yaml

import {pkg}
from {pkg} import PLUGIN, {tool}

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
| `tests/` | Contract tests; run with `pytest` |
| `.github/workflows/ci.yml` | Tests, builds the wheel, prints its sha256 |

## Rules

- Every path in `genus-plugin.yaml` is relative to that file and must be
  package data. `pyproject.toml` already covers `agents/`, `workflows/`,
  `patches/` and `files/`; put new files there, or add the directory to
  `[tool.setuptools.package-data]`.
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


def _files(name: str) -> dict[str, str]:
    pkg = package_name(name)
    tool = tool_name(name)
    values = {
        "name": name,
        "pkg": pkg,
        "tool": tool,
        "upper": tool.upper(),
        "title": name.replace("-", " ").title(),
    }
    return {
        "pyproject.toml": _PYPROJECT.format(**values),
        "README.md": _README.format(**values),
        "CLAUDE.md": _CLAUDE.format(**values),
        ".gitignore": _GITIGNORE,
        ".github/workflows/ci.yml": _WORKFLOW,
        f"src/{pkg}/__init__.py": _INIT.format(**values),
        f"src/{pkg}/genus-plugin.yaml": _MANIFEST.format(**values),
        f"src/{pkg}/agents/{name}/manifest.template.yaml": _AGENT_MANIFEST.format(**values),
        f"src/{pkg}/agents/{name}/instructions.template.md": _INSTRUCTIONS.format(**values),
        f"src/{pkg}/workflows/.gitkeep": "",
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
