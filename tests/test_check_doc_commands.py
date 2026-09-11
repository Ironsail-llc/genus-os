"""The gate that keeps every CLI command quoted in the docs real.

The published Getting Started opened with `pip install robothor` -- a package
that does not exist; the distribution is `genusos`. Nothing caught it because
nothing has ever executed, or even parsed, a command printed in the docs.

This checker parses each one against the real `robothor.cli` parser, so a
renamed or deleted subcommand fails the build in the same PR that removes it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_doc_commands.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_doc_commands", SCRIPT)
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


def test_valid_command_passes(checker) -> None:
    text = "```bash\nrobothor status\n```\n"
    assert checker.check_markdown(text, "doc.md") == []


def test_valid_command_with_flags_and_prompt_passes(checker) -> None:
    text = "```console\n$ robothor serve --host 0.0.0.0 --port 9099  # start the API\n```\n"
    assert checker.check_markdown(text, "doc.md") == []


def test_placeholders_are_substituted_before_parsing(checker) -> None:
    angle = '```bash\nrobothor agent scaffold <agent-id> --description "does a thing"\n```\n'
    braced = "```bash\nrobothor init --workspace ${ROBOTHOR_WORKSPACE}\n```\n"
    bare = "```bash\nrobothor init --workspace $ROBOTHOR_WORKSPACE\n```\n"
    for text in (angle, braced, bare):
        assert checker.check_markdown(text, "doc.md") == [], text


def test_unknown_subcommand_fails(checker) -> None:
    text = "```bash\nrobothor teleport --to mars\n```\n"
    findings = checker.check_markdown(text, "doc.md")
    assert len(findings) == 1
    assert findings[0].line == 2
    assert "teleport" in findings[0].command
    assert "does not parse" in findings[0].reason


def test_unknown_flag_on_a_valid_subcommand_fails(checker) -> None:
    """`parse_known_args` ACCEPTS unknown flags -- it returns them as extras.

    Discarding that return made the checker blind to exactly the class of
    error this gate exists for: `robothor migrate --status` parsed clean at a
    time when no such flag existed. (It exists now — the one-migrator change
    added it — which is why the negative cases below use a spelling nobody is
    going to implement.)
    """
    text = "```bash\nrobothor status --totally-bogus-flag\n```\n"
    findings = checker.check_markdown(text, "doc.md")
    assert len(findings) == 1
    assert "does not parse" in findings[0].reason


def test_a_flag_that_does_not_exist_on_migrate_fails(checker) -> None:
    """The example flag must be one `migrate` genuinely does not accept.

    This used `--status`, which this branch then added to `migrate` — so the
    test started asserting that a real flag fails, and went red for being
    right. Pick a spelling nobody will implement; a gate whose negative case
    can be satisfied by shipping a feature is not testing the gate.
    """
    assert (
        len(checker.check_markdown("```bash\nrobothor migrate --no-such-flag\n```\n", "doc.md"))
        == 1
    )
    # Positive controls, so the gate cannot go blind in the other direction by
    # rejecting flags that do exist.
    for flag in ("--check", "--status", "--adopt-baseline", "--adopt-through 001_init"):
        assert checker.check_markdown(f"```bash\nrobothor migrate {flag}\n```\n", "doc.md") == []


def test_help_is_not_a_failure(checker) -> None:
    """argparse exits 0 for --help and 2 for a parse error.

    Treating every SystemExit as failure reported a documented `--help` as
    broken, which would have pushed authors toward the skip marker.
    """
    assert checker.check_markdown("```bash\nrobothor --help\n```\n", "doc.md") == []
    assert checker.check_markdown("```bash\nrobothor snapshot --help\n```\n", "doc.md") == []
    assert len(checker.check_markdown("```bash\nrobothor nonesuch\n```\n", "doc.md")) == 1


def test_untagged_block_starting_with_a_cli_command_is_checked(checker) -> None:
    text = "```\nrobothor teleport\n```\n"
    findings = checker.check_markdown(text, "doc.md")
    assert len(findings) == 1


def test_untagged_prose_block_is_not_checked(checker) -> None:
    text = "```\nSome output that mentions robothor teleport somewhere\n```\n"
    assert checker.check_markdown(text, "doc.md") == []


def test_pip_install_robothor_fails(checker) -> None:
    text = "```bash\npip install robothor\n```\n"
    findings = checker.check_markdown(text, "doc.md")
    assert len(findings) == 1
    assert "genusos" in findings[0].reason


def test_pip_install_robothor_with_extras_fails(checker) -> None:
    text = "```bash\nsudo -u robothor pip install robothor[all]\n```\n"
    assert len(checker.check_markdown(text, "doc.md")) == 1


def test_pip_install_genusos_passes(checker) -> None:
    text = '```bash\npip install "genusos[api]"\n```\n'
    assert checker.check_markdown(text, "doc.md") == []


def test_skip_marker_before_the_fence_skips_the_block(checker) -> None:
    text = "<!-- doc-check: skip -->\n```bash\nrobothor teleport\n```\n"
    assert checker.check_markdown(text, "doc.md") == []


def test_skip_marker_only_skips_the_block_it_precedes(checker) -> None:
    text = (
        "<!-- doc-check: skip -->\n```bash\nrobothor teleport\n```\n\n```bash\nrobothor warp\n```\n"
    )
    findings = checker.check_markdown(text, "doc.md")
    assert len(findings) == 1
    assert "warp" in findings[0].command


def test_genusos_console_script_is_recognised(checker) -> None:
    assert checker.check_markdown("```bash\ngenusos status\n```\n", "doc.md") == []
    assert len(checker.check_markdown("```bash\ngenusos teleport\n```\n", "doc.md")) == 1


def test_shell_operators_are_truncated_not_parsed(checker) -> None:
    text = "```bash\nrobothor status && echo ok\n```\n"
    assert checker.check_markdown(text, "doc.md") == []


def test_the_repository_documentation_passes(checker) -> None:
    findings = [
        str(finding)
        for path in checker.documents(REPO_ROOT)
        for finding in checker.check_file(path, REPO_ROOT)
    ]
    assert not findings, "documented commands that do not exist:\n" + "\n".join(findings)
