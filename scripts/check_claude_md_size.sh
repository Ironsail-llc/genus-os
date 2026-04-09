#!/usr/bin/env bash
# Pre-commit hook: enforce max 80 lines per CLAUDE.md (except templates/)
ok=true
for f in $(git diff --cached --name-only | grep "CLAUDE.md" | grep -v "templates/" | grep -v "^CLAUDE.md$"); do
  lines=$(wc -l < "$f")
  if [ "$lines" -gt 80 ]; then
    echo "ERROR: $f is $lines lines (max 80)"
    ok=false
  fi
done
$ok
