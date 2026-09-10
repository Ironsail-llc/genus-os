#!/usr/bin/env python3
"""CI gate: every path the shipped docs point at must exist in git.

`docs/READING_GUIDE.md` ships on the public documentation site. Eight of its
rows pointed into `brain/` -- the gitignored instance workspace, absent from
every clean checkout -- and two more pointed at files (`INFRASTRUCTURE.md`,
`brain/memory_system/MEMORY_SYSTEM.md`) that exist nowhere at all. Nothing
noticed, because no gate had ever resolved a path printed in the docs.

Two shapes are resolved against `git ls-files`:

  * relative markdown links -- `[Deployment](deployment.md)`
  * backticked repo paths  -- `` `robothor/engine/` ``

A reference into one of the gitignored instance trees of CLAUDE.md rule #11
(`brain/`, `docs/agents/`, `docs/CRON_MAP.md`, …) is legitimate only when the
line says so: an instance path is not a shipped file, and a reader must be
told which is which. The rule is mechanical -- the surrounding line has to
contain "instance". A tracked path always wins over the prefix, so
`docs/agents/schema.yaml` resolves normally.

Exit code 0 = every documented path resolves, 1 = at least one does not.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Top-level trees a backticked reference may name. `brain/` is included on
# purpose: it is the one that must carry the instance label, and a checker
# that could not see it would enforce nothing.
PATH_ROOTS = ("docs", "robothor", "crm", "infra", "scripts", "templates", "helm", "app", "brain")

BACKTICK_RE = re.compile(r"`([^`\n]+)`")
PATH_REFERENCE_RE = re.compile(rf"^(?:{'|'.join(PATH_ROOTS)})/[\w./-]+$")

# [text](target) -- target captured up to the closing paren.
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

# Link targets that are not repository paths.
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "//", "/")

# The gitignored trees of CLAUDE.md rule #11 -- instance data, absent from a
# clean checkout by design. A path here is not "missing"; it is somebody
# else's. The line has to say so, or a reader is sent to a file they will
# never have. (`docs/agents/schema.yaml` and its siblings ARE tracked and are
# resolved normally: tracked always wins over the prefix.)
INSTANCE_PATH_PREFIXES = (
    "brain/",
    "docs/agents/",
    "docs/workflows/delphi/",
    "docs/experiments/",
    "docs/CRON_MAP.md",
    "local/",
    ".robothor/",
)
INSTANCE_LABEL = "instance"

# Same spelling as the command checker's escape hatch. Here it covers the
# markdown block that follows it, for paths that are deliberately absent --
# a roadmap of tests nobody has written, or an archived plan.
SKIP_MARKER = "<!-- doc-check: skip -->"

TOP_LEVEL_DOCS = ("README.md", "SERVICES.md", "CONTRIBUTING.md")

# Dated session artifacts: a plan written on one day, never edited again, and
# not part of the shipped documentation. `scripts/check_instance_leak.py`
# already exempts this tree for the same reason -- rewriting a historical
# record to satisfy a gate makes the record less true, not more.
ARCHIVED_DOC_DIRS = ("docs/superpowers/",)


@dataclass(frozen=True)
class Finding:
    """One documented path that does not resolve."""

    path: str
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.target} -- {self.reason}"


def known_paths(repo_root: Path) -> frozenset[str]:
    """Every tracked file, plus every directory containing one."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        tracked = line.strip()
        if not tracked:
            continue
        paths.add(tracked)
        paths.update(_ancestor_directories(tracked))
    return frozenset(paths)


def _ancestor_directories(path: str) -> list[str]:
    """Every ancestor directory of a repo-relative path."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: index + 1]) for index in range(len(parts))]


def documents(repo_root: Path) -> list[Path]:
    """Every markdown file this gate checks, in a stable order."""
    paths = [
        path
        for path in sorted((repo_root / "docs").rglob("*.md"))
        if not str(path.relative_to(repo_root)).startswith(ARCHIVED_DOC_DIRS)
    ]
    paths.extend(repo_root / name for name in TOP_LEVEL_DOCS)
    # The runnable examples are the second thing a new user copies from, and
    # all three of them opened with `pip install robothor` too.
    paths.extend(sorted((repo_root / "examples").glob("*/README.md")))
    return [path for path in paths if path.is_file()]


def _normalise(target: str) -> str | None:
    """Strip fragments/queries; None when the target is not a repo path."""
    cleaned = target.strip().strip("<>")
    if not cleaned or cleaned.startswith(EXTERNAL_PREFIXES):
        return None
    cleaned = cleaned.split("#", 1)[0].split("?", 1)[0]
    return cleaned or None


def _resolve(target: str, doc_path: str) -> str | None:
    """A link target as a repo-relative POSIX path, or None if it escapes the repo.

    Resolved textually, never on disk: `Path.resolve` would consult the
    filesystem, and this gate must answer from the git tree alone -- otherwise
    a path that exists only on the author's machine passes.
    """
    base = Path(doc_path).parent
    parts: list[str] = []
    for part in (*base.parts, *target.split("/")):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def _exists(candidate: str, known: frozenset[str]) -> bool:
    return candidate.rstrip("/") in known


def _skipped_lines(lines: list[str]) -> set[int]:
    """Line numbers suppressed by a skip marker.

    The marker covers the markdown block it introduces -- everything down to
    the next blank line. That is exactly one table or one paragraph, so a
    roadmap of files nobody has written yet can be marked once, in the diff,
    rather than one row at a time. It is for paths that are deliberately not
    in the tree (planned or historical); a path that SHOULD exist gets fixed.
    """
    skipped: set[int] = set()
    for index, line in enumerate(lines):
        if line.strip() != SKIP_MARKER:
            continue
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].strip():
            skipped.add(cursor + 1)
            cursor += 1
    return skipped


def check_markdown(text: str, path: str, known: frozenset[str]) -> list[Finding]:
    """Every documented path in `text` that does not resolve in the git tree."""
    findings: list[Finding] = []
    lines = text.splitlines()
    skipped = _skipped_lines(lines)
    for lineno, line in enumerate(lines, start=1):
        if lineno in skipped:
            continue
        labelled_instance = INSTANCE_LABEL in line.lower()

        targets: list[str] = []
        for match in MARKDOWN_LINK_RE.finditer(line):
            normalised = _normalise(match.group(1))
            if normalised is None:
                continue
            resolved = _resolve(normalised, path)
            if resolved is not None:
                targets.append(resolved)

        for match in BACKTICK_RE.finditer(line):
            reference = match.group(1).strip()
            if PATH_REFERENCE_RE.match(reference):
                targets.append(reference)

        for target in targets:
            if _exists(target, known):
                continue
            if target.startswith(INSTANCE_PATH_PREFIXES):
                if not labelled_instance:
                    findings.append(
                        Finding(
                            path=path,
                            line=lineno,
                            target=target,
                            reason=(
                                "instance-local path -- say so on the line, or point at "
                                "the platform document instead"
                            ),
                        )
                    )
                continue
            findings.append(Finding(path=path, line=lineno, target=target, reason="missing path"))
    return findings


def check_file(path: Path, repo_root: Path, known: frozenset[str]) -> list[Finding]:
    """Check one markdown file, reporting paths relative to the repo root."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return check_markdown(text, str(path.relative_to(repo_root)), known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repository root to scan (default: the repo this script lives in)",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    known = known_paths(repo_root)
    files = documents(repo_root)
    findings: list[Finding] = []
    for path in files:
        findings.extend(check_file(path, repo_root, known))

    if findings:
        print("DOCUMENTED PATHS THAT DO NOT RESOLVE:")
        for finding in findings:
            print(f"  {finding}")
        print(
            f"\n{len(findings)} issue(s). Point at a tracked file, or -- if the path really is "
            f"instance data -- say so on the line."
        )
        return 1

    print(f"check_doc_links: {len(files)} file(s) clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
