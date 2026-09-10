"""Two defects the documentation gate surfaced but could not fix from markdown.

1. ``robothor agent install --preset standard`` — the documented preset form —
   was rejected by argparse because ``source`` was a required positional that
   preset mode then ignored. The docs quoted a command that could not run.
2. Several runtime messages told the operator to ``pip install robothor``;
   the distribution is ``genusos`` (``pyproject.toml``), so the advice failed
   at the first command a stuck operator would try.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import TYPE_CHECKING

from robothor.cli import _build_parser

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OLD_DISTRIBUTION = re.compile(r"pip install ['\"]?robothor\b")


def test_agent_install_preset_needs_no_source() -> None:
    args = _build_parser().parse_args(["agent", "install", "--preset", "standard"])
    assert args.preset == "standard"
    assert args.source is None


def test_agent_install_source_still_parses_positionally() -> None:
    args = _build_parser().parse_args(["agent", "install", "email-triage"])
    assert args.source == "email-triage"
    assert args.preset is None


def test_agent_install_without_source_or_preset_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from robothor.cli.agent import _cmd_agent_install

    args = argparse.Namespace(source=None, preset=None, yes=True, set=[])
    assert _cmd_agent_install(args) == 1
    out = capsys.readouterr().out
    assert "source" in out and "--preset" in out


def test_no_runtime_message_names_the_old_distribution() -> None:
    offenders: list[str] = []
    for path in REPO_ROOT.glob("robothor/**/*.py"):
        if "/tests/" in path.as_posix():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OLD_DISTRIBUTION.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "install advice names a distribution that does not exist:\n" + "\n".join(
        offenders
    )
