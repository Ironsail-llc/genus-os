"""No agent template may name a model. The fleet's defaults decide.

A template that hardcodes `ollama/qwen3.5:122b` installs an instance pinned to
a model the operator never chose and may not have — and the wizard's provider
step, which just probed a real credential and got a real completion, is
overruled by a placeholder written months earlier. On a fresh compose install
that is exactly what happened: `genus run --agent main` walked
`ollama/qwen3.5:122b` (dialling a localhost Ollama that does not exist inside
a container), then three cloud models the box had no key for, and reported
"All models failed to respond".

Installed manifests carry no `model:` block at all. They inherit
`<workspace>/docs/agents/_defaults.yaml`, which `genus init` writes from the
provider the operator actually chose.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPO_ROOT / "templates" / "agents"

#: The litellm route prefixes. A literal model id in a template is one of
#: these followed by a name; a bare word is a tier, which is allowed.
MODEL_ID = re.compile(
    r"\b(?:ollama_chat|ollama|openrouter|anthropic|openai|codex|gemini|litellm_proxy)/[\w.:@-]+"
)

#: Prose that talks ABOUT routing without pinning an instance to a model.
ALLOWED = re.compile(r"ollama_chat/<|openrouter/<|ollama/<")


def _offenders() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(TEMPLATE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".md", ".json"}:
            continue
        hits = [
            match.group(0)
            for line in path.read_text(encoding="utf-8").splitlines()
            if not ALLOWED.search(line)
            for match in MODEL_ID.finditer(line)
        ]
        if hits:
            found[str(path.relative_to(REPO_ROOT))] = sorted(set(hits))
    return found


def test_no_template_names_a_model_id() -> None:
    offenders = _offenders()

    assert not offenders, (
        "these agent templates name a model id, so an install is pinned to a model "
        "the operator never chose:\n  "
        + "\n  ".join(f"{path}: {', '.join(models)}" for path, models in offenders.items())
        + "\nInstalled manifests inherit docs/agents/_defaults.yaml, which the wizard "
        "writes from the provider the operator picked. Use a named tier, never a literal id."
    )


def test_no_manifest_template_declares_a_model_block() -> None:
    """Not even an empty one: a `model:` key in an installed manifest wins over
    the fleet defaults, which is the whole mechanism this removes."""
    import yaml

    declaring = []
    for path in sorted(TEMPLATE_ROOT.rglob("manifest.template.yaml")):
        # The templates carry `{{ }}` placeholders, so they are read as text.
        body = path.read_text(encoding="utf-8")
        if re.search(r"^model:\s*$", body, re.MULTILINE):
            declaring.append(str(path.relative_to(REPO_ROOT)))
    del yaml

    assert not declaring, (
        "these manifest templates declare a `model:` block, which overrides the "
        "fleet defaults on every install:\n  " + "\n  ".join(declaring)
    )


def test_the_template_defaults_declare_no_model() -> None:
    import yaml

    defaults = yaml.safe_load((TEMPLATE_ROOT / "_defaults.yaml").read_text(encoding="utf-8")) or {}

    named = sorted(key for key in defaults if key.startswith("model"))
    assert not named, (
        f"templates/agents/_defaults.yaml still declares {named} — the fleet's own "
        "docs/agents/_defaults.yaml is where a model belongs"
    )


def test_no_setup_yaml_declares_a_model_variable() -> None:
    import yaml

    declaring: dict[str, list[str]] = {}
    for path in sorted(TEMPLATE_ROOT.rglob("setup.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        named = sorted(
            key for key in (document.get("variables") or {}) if str(key).startswith("model")
        )
        if named:
            declaring[str(path.relative_to(REPO_ROOT))] = named

    assert not declaring, (
        "these setup.yaml files declare a model variable, so the installer "
        "substitutes a model id into the manifest it writes:\n  "
        + "\n  ".join(f"{path}: {', '.join(keys)}" for path, keys in declaring.items())
    )
