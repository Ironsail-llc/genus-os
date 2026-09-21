"""The annotation backlog in mypy.ini is a ratchet, not a hiding place.

`robothor/sales/` arrived whole in #624 without annotations, and 178 new
unannotated files took CI's `mypy robothor/ --ignore-missing-imports` from
green to 1,589 errors. The response was NOT a wildcard: every new module added
to an already-typed package was annotated, every semantic error was fixed, and
what remains is an explicit per-module list that relaxes annotation COVERAGE
only.

Two properties have to hold or that list becomes the blanket ignore it was
written to avoid, and this file pins both:

1. It may only shrink. An entry whose module no longer needs it is dead
   weight, and dead entries are how a list stops describing anything.
2. It may only relax coverage, and only for the package it was written for.
   `ignore_errors`, or a wildcard, would hide the next real error.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "mypy.ini"

#: The only checks an entry is allowed to turn off. Each one is about whether a
#: signature is written down, not about whether the code is right.
COVERAGE_ONLY = frozenset(
    {
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "disallow_untyped_calls",
        "disallow_untyped_decorators",
    }
)

#: The package the backlog was opened for. A section outside it means an
#: already-typed package started relaxing instead of annotating.
BACKLOG_PACKAGE = "robothor.sales."


def _sections() -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser()
    parser.read(_CONFIG, encoding="utf-8")
    return {name: dict(parser[name]) for name in parser.sections()}


def backlog() -> dict[str, dict[str, str]]:
    return {
        name.removeprefix("mypy-"): body
        for name, body in _sections().items()
        if name.startswith("mypy-") and COVERAGE_ONLY & set(body)
    }


def test_the_backlog_is_confined_to_the_package_it_was_opened_for():
    outside = sorted(m for m in backlog() if not m.startswith(BACKLOG_PACKAGE))

    assert outside == [], (
        "every other package in this tree is fully annotated and CI is green on it; "
        "annotate the new module instead of adding it here"
    )


def test_no_entry_is_a_wildcard():
    """`robothor.sales.*` would swallow every module added next."""
    globbed = sorted(m for m in backlog() if "*" in m)

    assert globbed == []


def test_no_entry_silences_a_check_that_finds_bugs():
    offenders = {
        module: sorted(set(body) - COVERAGE_ONLY)
        for module, body in backlog().items()
        if set(body) - COVERAGE_ONLY
    }

    assert offenders == {}, (
        "an entry may relax annotation coverage and nothing else — ignore_errors, "
        "or switching off a semantic check, hides the next real error"
    )


def test_every_listed_module_exists():
    missing = sorted(
        module for module in backlog() if not (_ROOT / (module.replace(".", "/") + ".py")).is_file()
    )

    assert missing == [], "delete the entry when you delete the module"


@pytest.mark.slow
def test_no_listed_module_has_already_been_annotated():
    """The shrink half of the ratchet: a clean module must leave the list.

    Runs the gate's own command with the backlog removed and keeps only the
    coverage diagnostics. A listed module that reports none of them no longer
    needs its entry.
    """
    listed = backlog()
    assert listed, "the backlog is empty — delete it and this test with it"

    stripped = _CONFIG.read_text(encoding="utf-8")
    parser = configparser.ConfigParser()
    parser.read_string(stripped)
    for module in listed:
        parser.remove_section("mypy-" + module)

    scratch = _ROOT / ".mypy-backlog-probe.ini"
    with scratch.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "robothor/",
                "--ignore-missing-imports",
                "--config-file",
                str(scratch),
            ],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    finally:
        scratch.unlink(missing_ok=True)

    codes = ("[no-untyped-def]", "[no-untyped-call]", "[untyped-decorator]", "[no-any-return]")
    still_needed = {
        line.split(":", 1)[0].removesuffix(".py").replace("/", ".")
        for line in result.stdout.splitlines()
        if line.endswith(codes)
    }
    annotated = sorted(module for module in listed if module not in still_needed)

    assert annotated == [], (
        "these modules are annotated now — remove their mypy.ini entries so the "
        "backlog keeps describing what is actually left"
    )
