#!/usr/bin/env python3
"""Pre-commit hook: detect instance-specific data in platform files.

Scans staged files for patterns that indicate personal identity, hardcoded
paths, or instance configuration that should live in brain/ or .env instead
of tracked platform code.

Exit code 0 = clean, 1 = leaks found.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Paths that are instance-specific — never scan these
INSTANCE_PATHS = {
    "brain/",
    "docs/agents/",
    "local/",
    ".robothor/",
    "templates/",
    ".env",
    "CHANGELOG.md",
    # Dated agent session transcripts — they quote the operator's shell history
    # verbatim and are never edited again. They are session artifacts, not
    # platform docs.
    "docs/superpowers/plans/",
}

# Generated/vendored files — skip entirely (package names false-positive as emails)
GENERATED_FILES = {
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "Pipfile.lock",
    "poetry.lock",
}

# Patterns to detect (compiled regexes)
LEAK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Hardcoded home directory paths
    (re.compile(r"/home/\w+/"), "hardcoded home path — use $ROBOTHOR_WORKSPACE or Path.home()"),
    (re.compile(r"/Users/\w+/"), "hardcoded home path — use $ROBOTHOR_WORKSPACE or Path.home()"),
    # Phone numbers (US format)
    (
        re.compile(r"\+?1?\s*[-.(]?\d{3}[-.)]\s*\d{3}[-.]?\d{4}"),
        "possible phone number — move to brain/CLAUDE.md or .env",
    ),
    # Street addresses
    (
        re.compile(
            r"\d+\s+\w+\s+(Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Way|Boulevard|Blvd)\b",
            re.IGNORECASE,
        ),
        "possible street address — move to brain/CLAUDE.md",
    ),
]

# systemd-tmpfiles.d(5) / sysusers.d(5) row: TYPE PATH MODE USER GROUP AGE ARG.
# The account columns are POSITIONAL — no `User=` prefix — so neither the
# unit-file convention nor the /home/<user>/ pattern can see an instance
# username there. infra/tmpfiles/robothor-restart.conf shipped exactly that way
# and every gate passed it.
#
# Matched by the SHAPE of the line, not by the word: the repo says "robothor"
# everywhere legitimately. The shape (single-letter type + absolute path +
# octal mode + two identifiers) is specific enough that prose, Python and
# `ls -l` output cannot match it.
TMPFILES_ROW_RE = re.compile(
    r"^\s*[a-zA-Z][+!=^\-]*\s+/\S+\s+(?:[0-7]{3,4}|-)\s+"
    r"(?P<user>[A-Za-z_][A-Za-z0-9_-]*|-)\s+"
    r"(?P<group>[A-Za-z_][A-Za-z0-9_-]*|-)(?:\s|$)"
)

# systemd unit account directive. Applied ONLY to unit-shaped files: in Python
# a bare `Group = None` would otherwise match.
UNIT_ACCOUNT_RE = re.compile(
    r"^\s*(?P<key>User|Group)\s*=\s*(?P<account>[A-Za-z_][A-Za-z0-9_-]*)\s*$"
)
UNIT_SUFFIXES = (".service", ".timer", ".path", ".socket", ".mount", ".target", ".slice")

# The only accounts a platform template may name. Mirrors the set in
# tests/test_install_units.py::test_repo_templates_use_canonical_spellings —
# `robothor` is the PLACEHOLDER rendered to $ROBOTHOR_SERVICE_USER at install
# time; postgres/root/nobody exist on every box; `-` means "leave it alone".
PERMITTED_ACCOUNTS = {"robothor", "postgres", "root", "nobody", "-"}

# Email pattern — checked separately with allowlist
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
SAFE_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "anthropic.com",  # Co-Authored-By lines
    "users.noreply.github.com",
}


def _load_allowlist() -> set[str]:
    """Load additional safe patterns from allowlist file."""
    allowlist_path = Path(__file__).parent / "instance_leak_allowlist.yaml"
    if not allowlist_path.exists():
        return set()
    patterns = set()
    for line in allowlist_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            # Simple line-based format: one pattern per line
            patterns.add(line)
    return patterns


def _is_instance_path(path: str) -> bool:
    """Check if a file path is in an instance-specific directory."""
    return any(path.startswith(prefix) or path == prefix.rstrip("/") for prefix in INSTANCE_PATHS)


def _check_file(path: str, content: str, allowlist: set[str]) -> list[str]:
    """Check a file's content for instance data leaks. Returns list of warnings."""
    warnings = []

    for line_num, line in enumerate(content.splitlines(), 1):
        # Skip comments that are clearly documentation references
        stripped = line.strip()
        if stripped.startswith("#") and "example" in stripped.lower():
            continue

        # Check regex patterns
        for pattern, message in LEAK_PATTERNS:
            if pattern.search(line):
                # Check allowlist
                if any(allow in line for allow in allowlist):
                    continue
                warnings.append(f"  {path}:{line_num} — {message}")
                break  # One warning per line

        if not any(allow in line for allow in allowlist):
            row = TMPFILES_ROW_RE.match(line)
            if row:
                for column in ("user", "group"):
                    account = row.group(column)
                    if account not in PERMITTED_ACCOUNTS:
                        warnings.append(
                            f"  {path}:{line_num} — tmpfiles {column} column names "
                            f"'{account}' — use the `robothor` placeholder "
                            "(rendered by scripts/render-unit.sh --tmpfiles)"
                        )

            if path.endswith(UNIT_SUFFIXES) or ".service.d/" in path:
                unit = UNIT_ACCOUNT_RE.match(line)
                if unit and unit.group("account") not in PERMITTED_ACCOUNTS:
                    warnings.append(
                        f"  {path}:{line_num} — {unit.group('key')}= names "
                        f"'{unit.group('account')}', an instance account — use "
                        "`robothor` (rendered to ROBOTHOR_SERVICE_USER)"
                    )

        # Check emails
        for email_match in EMAIL_RE.finditer(line):
            email = email_match.group(0).lower()
            domain = email.split("@", 1)[1] if "@" in email else ""
            if domain in SAFE_EMAIL_DOMAINS:
                continue
            if any(allow in email for allow in allowlist):
                continue
            warnings.append(
                f"  {path}:{line_num} — email address '{email}' — use @example.com for test data"
            )

    return warnings


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def _target_files(mode: str, base: str | None) -> list[tuple[str, str | None]]:
    """``(path, git_rev_or_None)`` for each file to scan.

    Three modes, one scanner:

    default  staged files, read from the index — the pre-commit hook.
    --ci     everything this branch changed against the merge base, read from
             the working tree (the checkout IS the PR head). Same set a
             reviewer sees in the diff, and the same set the hook sees, so
             local and CI agree.
    --all    every tracked file. A deliberate cleanup pass, NOT a merge gate:
             the tree carries pre-existing warnings (mostly test-fixture
             emails) that would make a whole-tree gate red on day one and
             disabled within a week.
    """
    if mode == "all":
        return [(f, None) for f in _git("ls-files").splitlines() if f]
    if mode == "ci":
        ref = base or os.environ.get("GITHUB_BASE_REF") or ""
        base_ref = f"origin/{ref}" if ref and not ref.startswith("origin/") else (ref or "")
        if not base_ref or not _git("rev-parse", "--verify", "--quiet", base_ref).strip():
            base_ref = "origin/main"
        if not _git("rev-parse", "--verify", "--quiet", base_ref).strip():
            # push-to-main run: a squash of an already-scanned PR. Cheap backstop.
            base_ref = "HEAD~1"
        changed = _git("diff", "--name-only", "--diff-filter=ACMR", f"{base_ref}...HEAD")
        return [(f, None) for f in changed.splitlines() if f]
    staged = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [(f, "") for f in staged.splitlines() if f]


def main() -> int:
    """Scan for instance data leaks (CLAUDE.md rule #1)."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--ci", action="store_true", help="scan files changed against the merge base"
    )
    group.add_argument("--all", action="store_true", help="scan every tracked file")
    parser.add_argument("--base", help="base ref for --ci (default origin/$GITHUB_BASE_REF)")
    args = parser.parse_args()

    mode = "ci" if args.ci else "all" if args.all else "staged"
    targets = _target_files(mode, args.base)

    if not targets:
        return 0

    allowlist = _load_allowlist()
    all_warnings: list[str] = []

    for path, rev in targets:
        if _is_instance_path(path):
            continue
        if Path(path).name in GENERATED_FILES:
            continue

        if rev == "":
            # Staged content, not the working tree.
            content_result = subprocess.run(
                ["git", "show", f":{path}"],
                capture_output=True,
                text=True,
            )
            if content_result.returncode != 0:
                continue
            content = content_result.stdout
        else:
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

        # Only check text files
        if "\x00" in content[:1024]:
            continue

        warnings = _check_file(path, content, allowlist)
        all_warnings.extend(warnings)

    if all_warnings:
        print("INSTANCE DATA LEAK — the following lines contain personal/instance data:")
        print("Move to brain/CLAUDE.md, .env, or use generic test fixtures.\n")
        for w in all_warnings:
            print(w)
        print(f"\n{len(all_warnings)} issue(s) found. See docs/PLATFORM_INSTANCE.md for guidance.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
