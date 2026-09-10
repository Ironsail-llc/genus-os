"""The gate that keeps every path the docs point at real.

`docs/READING_GUIDE.md` is a published page. Eight of its twenty-five rows
pointed into `brain/` -- the gitignored instance workspace, absent from every
clean checkout -- and two more pointed at `INFRASTRUCTURE.md` and
`brain/memory_system/MEMORY_SYSTEM.md`, which do not exist anywhere.

A path in the docs is a claim about the tree, so it is checked against
`git ls-files`. A `brain/` path is allowed only where the line says it is
instance-local, so a reader is never sent to a file they cannot have.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_doc_links.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_doc_links", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the script's dataclasses use PEP 563 annotations,
    # which `dataclasses` resolves through `sys.modules[cls.__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _module()


@pytest.fixture(scope="module")
def known(checker):
    return checker.known_paths(REPO_ROOT)


def test_existing_relative_markdown_link_passes(checker, known) -> None:
    text = "See [the deployment guide](deployment.md).\n"
    assert checker.check_markdown(text, "docs/quickstart.md", known) == []


def test_missing_relative_markdown_link_fails(checker, known) -> None:
    text = "See [the old guide](INFRASTRUCTURE.md).\n"
    findings = checker.check_markdown(text, "docs/quickstart.md", known)
    assert len(findings) == 1
    assert findings[0].line == 1
    assert "INFRASTRUCTURE.md" in findings[0].target


def test_external_links_and_anchors_are_ignored(checker, known) -> None:
    text = (
        "[repo](https://github.com/Ironsail-llc/genus-os) "
        "[mail](mailto:security@example.com) "
        "[section](#configuration)\n"
    )
    assert checker.check_markdown(text, "docs/quickstart.md", known) == []


def test_existing_backticked_path_reference_passes(checker, known) -> None:
    text = "Config lives in `robothor/constants.py` and `docs/PLATFORM_INSTANCE.md`.\n"
    assert checker.check_markdown(text, "docs/quickstart.md", known) == []


def test_missing_backticked_path_reference_fails(checker, known) -> None:
    text = "Contact resolution is in `crm/bridge/contact_resolver.py`.\n"
    findings = checker.check_markdown(text, "docs/READING_GUIDE.md", known)
    assert len(findings) == 1
    assert "contact_resolver" in findings[0].target


def test_backticked_directory_reference_passes(checker, known) -> None:
    text = "The engine lives in `robothor/engine/`.\n"
    assert checker.check_markdown(text, "docs/READING_GUIDE.md", known) == []


def test_prose_backticks_are_not_treated_as_paths(checker, known) -> None:
    text = "Set `ROBOTHOR_WORKSPACE` and run `robothor status` first.\n"
    assert checker.check_markdown(text, "docs/quickstart.md", known) == []


def test_brain_reference_without_the_instance_label_fails(checker, known) -> None:
    text = "| Robothor's identity | `brain/SOUL.md` |\n"
    findings = checker.check_markdown(text, "docs/READING_GUIDE.md", known)
    assert len(findings) == 1
    assert "brain/SOUL.md" in findings[0].target


def test_instance_labelled_brain_reference_passes(checker, known) -> None:
    text = "| Identity | `brain/SOUL.md` (instance-local, not shipped) |\n"
    assert checker.check_markdown(text, "docs/READING_GUIDE.md", known) == []


def test_instance_labelled_brain_markdown_link_passes(checker, known) -> None:
    text = "See [the soul file](../brain/SOUL.md) -- instance-local, not shipped.\n"
    assert checker.check_markdown(text, "docs/READING_GUIDE.md", known) == []


def test_skip_marker_suppresses_the_block_it_precedes(checker, known) -> None:
    text = (
        "<!-- doc-check: skip -->\n"
        "| Planned | `crm/tests/test_not_written_yet.py` |\n"
        "| Planned | `crm/tests/test_also_planned.py` |\n"
    )
    assert checker.check_markdown(text, "docs/TESTING.md", known) == []


def test_skip_marker_stops_at_the_next_blank_line(checker, known) -> None:
    text = (
        "<!-- doc-check: skip -->\n"
        "| Planned | `crm/tests/test_not_written_yet.py` |\n"
        "\n"
        "| Live | `crm/tests/test_missing.py` |\n"
    )
    findings = checker.check_markdown(text, "docs/TESTING.md", known)
    assert len(findings) == 1
    assert findings[0].line == 4


def test_the_repository_documentation_passes(checker, known) -> None:
    findings = [
        str(finding)
        for path in checker.documents(REPO_ROOT)
        for finding in checker.check_file(path, REPO_ROOT, known)
    ]
    assert not findings, "documented paths that do not exist:\n" + "\n".join(findings)
