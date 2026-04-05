#!/usr/bin/env bash
# Pre-commit hook: warn if deleted .py files are still referenced elsewhere.
# Catches cases like triage_prep.py being deleted while crontab/other scripts
# still import or call it.

set -euo pipefail

# Get list of deleted .py files in this commit
deleted_files=$(git diff --cached --diff-filter=D --name-only -- '*.py' 2>/dev/null || true)

if [ -z "$deleted_files" ]; then
    exit 0
fi

warnings=0

for filepath in $deleted_files; do
    basename=$(basename "$filepath" .py)
    # Skip __init__, conftest, test files
    case "$basename" in
        __init__|conftest|test_*) continue ;;
    esac
    # Skip brain/ and docs/agents/ — these are instance-data (.gitignored)
    case "$filepath" in
        brain/*|docs/agents/*|docs/CRON_MAP*) continue ;;
    esac

    # Search for references in crontab
    cron_refs=$(crontab -l 2>/dev/null | grep -c "$basename" || true)

    # Search for imports/references in other .py files (excluding the deleted file itself)
    # Only search tracked files to avoid false positives from .gitignored instance data and venvs
    code_refs=$(git ls-files '*.py' | grep -v "$filepath" \
        | xargs grep -l -E "(import ${basename}|from.*${basename}|${basename}\.py)" 2>/dev/null \
        | head -5 || true)

    # Search in YAML manifests and shell scripts (tracked files only)
    config_refs=$(git ls-files '*.yaml' '*.yml' '*.sh' \
        | xargs grep -l "$basename" 2>/dev/null \
        | head -5 || true)

    if [ "$cron_refs" -gt 0 ] || [ -n "$code_refs" ] || [ -n "$config_refs" ]; then
        echo "WARNING: Deleted '$filepath' is still referenced:"
        if [ "$cron_refs" -gt 0 ]; then
            echo "  - crontab: $cron_refs reference(s)"
        fi
        if [ -n "$code_refs" ]; then
            echo "  - code:"
            echo "$code_refs" | sed 's/^/      /'
        fi
        if [ -n "$config_refs" ]; then
            echo "  - config:"
            echo "$config_refs" | sed 's/^/      /'
        fi
        warnings=$((warnings + 1))
    fi
done

if [ "$warnings" -gt 0 ]; then
    echo ""
    echo "$warnings deleted file(s) have dangling references."
    echo "Verify these references are cleaned up before committing."
    exit 1
fi

exit 0
