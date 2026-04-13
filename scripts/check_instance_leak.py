#!/usr/bin/env python3
"""Pre-commit hook: detect instance-specific data in platform files.

Scans staged files for patterns that indicate personal identity, hardcoded
paths, or instance configuration that should live in brain/ or .env instead
of tracked platform code.

Exit code 0 = clean, 1 = leaks found.
"""

from __future__ import annotations

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


def main() -> int:
    """Scan staged files for instance data leaks."""
    # Get list of staged files
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
    )
    staged_files = [f for f in result.stdout.strip().splitlines() if f]

    if not staged_files:
        return 0

    allowlist = _load_allowlist()
    all_warnings: list[str] = []

    for path in staged_files:
        if _is_instance_path(path):
            continue
        if Path(path).name in GENERATED_FILES:
            continue

        # Read staged content (not working tree)
        content_result = subprocess.run(
            ["git", "show", f":{path}"],
            capture_output=True,
            text=True,
        )
        if content_result.returncode != 0:
            continue

        # Only check text files
        if "\x00" in content_result.stdout[:1024]:
            continue

        warnings = _check_file(path, content_result.stdout, allowlist)
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
